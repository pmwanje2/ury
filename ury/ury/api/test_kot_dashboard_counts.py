"""Site-less H3 regression: execute the real API with an in-memory read boundary."""

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


class Row(dict):
    __getattr__ = dict.__getitem__


class TestKitchenDashboardCounts(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 15)
        self.rows = []
        self.production_unit = Row(enable_order_type_wise_display_on_mosaic=0)
        self.frappe = MagicMock()
        self.frappe.whitelist.side_effect = lambda **kwargs: lambda function: function
        self.frappe.session.user = 'captain@example.com'
        self.frappe.get_roles.return_value = ['URY Captain']
        self.frappe.get_list.side_effect = self.get_list
        self.frappe.get_all.side_effect = self.get_list
        self.frappe.get_doc.side_effect = self.get_doc
        self.frappe.as_json.side_effect = json.dumps
        self.frappe.db.get_value.return_value = 0
        self.frappe.utils.now.return_value = self.now.isoformat(' ')
        self.frappe.utils.add_to_date.side_effect = lambda value, hours: (
            datetime.fromisoformat(value) + timedelta(hours=hours)
        ).isoformat(' ')
        utils = ModuleType('frappe.utils')
        utils.get_datetime = lambda value=None: datetime.fromisoformat(value) if value else self.now
        api = ModuleType('ury.ury_pos.api')
        api.getBranch = MagicMock(return_value='Branch A')
        self.enterContext(patch.dict(sys.modules, {
            'frappe': self.frappe, 'frappe.utils': utils, 'ury.ury_pos.api': api,
        }))
        spec = importlib.util.spec_from_file_location('kot_counts_under_test', Path(__file__).with_name('ury_kot_display.py'))
        self.display = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.display)

    def ticket(self, name, **changes):
        row = Row(name=name, production='Kitchen', branch='Branch A', type='New Order',
                  docstatus=1, verified=0, creation='2026-11-12 14:00:00',
                  order_status='Ready For Prepare', original_kot='', invoice='POS-1')
        row.update(changes)
        self.rows.append(row)
        return row

    def get_list(self, doctype, *, filters, fields, **kwargs):
        self.assertEqual(doctype, 'URY KOT')
        def matches(row):
            for key, expected in filters.items():
                if isinstance(expected, (list, tuple)):
                    op, value = expected
                    if op == 'in' and row[key] not in value:
                        return False
                    if op == '>=' and row[key] < value:
                        return False
                elif row[key] != expected:
                    return False
            return True
        return [Row({key: row[key] for key in fields}) for row in self.rows if matches(row)]

    def get_doc(self, doctype, name):
        if doctype == 'URY Production Unit':
            return self.production_unit
        return next(row for row in self.rows if row.name == name)

    def counts(self):
        counts = getattr(self.display, 'production_unit_counts', None)
        self.assertTrue(callable(counts), 'Overview must use the windowed production_unit_counts API, not all-time getCount')
        return counts('["Kitchen"]')['Kitchen']

    def test_five_old_verified_cancellations_are_not_active(self):
        for index in range(5):
            self.ticket(f'CNCL-{index}', type='Cancelled', verified=1, creation='2026-11-10 11:05:00')
        self.assertEqual(self.display.kot_list()['KOT'], [])
        self.assertEqual(self.counts(), {'active_orders': 0, 'served_orders': 0, 'total_orders': 0})

    def test_counts_match_the_board_window_verification_type_and_branch(self):
        self.ticket('ACTIVE')
        self.ticket('CANCEL-PENDING', type='Cancelled')
        self.ticket('BOUNDARY', creation='2026-11-12 12:00:00')
        self.ticket('SERVED', order_status='Served')
        self.ticket('OLD', creation='2026-11-12 11:59:59')
        self.ticket('VERIFIED', type='Cancelled', verified=1)
        self.ticket('FOREIGN', branch='Branch B')
        self.ticket('VOID', docstatus=2)
        self.ticket('OTHER-TYPE', type='Not a board ticket')
        board = self.display.kot_list()['KOT']
        self.assertEqual({row['name'] for row in board}, {'ACTIVE', 'CANCEL-PENDING', 'BOUNDARY'})
        self.assertEqual(self.counts(), {'active_orders': 3, 'served_orders': 1, 'total_orders': 4})
        self.assertEqual(len(self.display.served_kot_list()['KOT']), 1)

    def test_confirmed_cancel_removes_its_original_from_board_and_card(self):
        self.ticket('ORIGINAL')
        self.ticket('CANCEL', type='Cancelled', verified=1, original_kot=' ORIGINAL, ')
        self.assertEqual(self.display.kot_list()['KOT'], [])
        self.assertEqual(self.counts()['active_orders'], 0)

    def test_counts_preserve_the_forks_production_order_type_filter(self):
        self.production_unit.update(enable_order_type_wise_display_on_mosaic=1,
                                    order_type=[SimpleNamespace(order_type='Dine In')])
        self.frappe.db.get_value.side_effect = lambda doctype, name, field: (
            {'POS-1': 'Dine In', 'POS-2': 'Take Away'}.get(name)
            if doctype == 'POS Invoice' else 0
        )
        self.ticket('DINE-IN')
        self.ticket('TAKE-AWAY', invoice='POS-2')
        self.ticket('SERVED-DINE-IN', order_status='Served')
        self.ticket('SERVED-TAKE-AWAY', invoice='POS-2', order_status='Served')
        self.assertEqual(len(self.display.kot_list()['KOT']), 1)
        self.assertEqual(len(self.display.served_kot_list()['KOT']), 1)
        self.assertEqual(self.counts(), {'active_orders': 1, 'served_orders': 1, 'total_orders': 2})

    def test_administrator_counts_stay_in_the_same_branch_as_the_fork_board(self):
        self.frappe.session.user = 'Administrator'
        self.frappe.get_roles.return_value = ['URY Admin']
        self.ticket('LOCAL')
        self.ticket('FOREIGN', branch='Branch B')
        self.assertEqual(len(self.display.kot_list()['KOT']), 1)
        self.assertEqual(self.counts()['active_orders'], 1)


if __name__ == '__main__':
    unittest.main()
