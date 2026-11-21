"""Closed checks are searchable with read permission and no POS opening."""
import unittest
from unittest.mock import patch

import frappe
from ury.ury_pos import api


class SettledBills(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(patch.object(api, 'frappe'))
        self.f.get_roles.return_value = ['URY Cashier']
        self.f.PermissionError = PermissionError
        self.f.throw.side_effect = lambda message, exc=ValueError: (_ for _ in ()).throw(exc(message))
        self.enterContext(patch.object(api, 'getBranch', return_value='West'))
        self.f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
        self.f.get_list.return_value = [frappe._dict(name='INV-1')]
        self.doc = frappe._dict(name='INV-1', branch='West', docstatus=1, status='Unpaid',
                                pos_profile='Bar', restaurant_table='1', outstanding_amount=25,
                                items=[dict(item_name='Soup', qty=1, amount=25)], taxes=[],
                                payments=[dict(mode_of_payment='Card', amount=0)])
        self.f.get_doc.return_value = self.doc
        self.enterContext(patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'))

    def endpoint(self, name):
        fn = getattr(api, name, None)
        self.assertTrue(callable(fn), f'Missing closed-bill API: {name}')
        return fn

    def test_previous_shift_and_on_account(self):
        rows = self.endpoint('search_settled_bills')(branch='West', date_from='2026-11-01',
            date_to='2026-11-07', bill_no='INV', table='1', pos_profile='Bar')
        filters = self.f.get_list.call_args.kwargs['filters']
        self.assertEqual(filters['docstatus'], 1)
        self.assertEqual(set(filters['status'][1]), {'Paid','Consolidated','Return','Partly Paid','Unpaid','Overdue'})
        self.assertEqual(filters['posting_date'][0], 'between')
        for key, value in [('branch','West'), ('restaurant_table','1'), ('pos_profile','Bar')]:
            self.assertEqual(filters[key], value)
        self.assertIn('INV', filters['name'][1])
        self.assertEqual(rows[0]['outstanding_amount'], 25)
        self.assertEqual(rows[0]['payments'][0]['mode_of_payment'], 'Card')
        self.assertEqual(rows[0]['items'][0]['item_name'], 'Soup')
        self.f.db.exists.assert_not_called()

    def test_read_branch_role_and_state_denials(self):
        search = self.endpoint('search_settled_bills')
        copy = self.endpoint('print_bill_copy')
        for change, value in [('branch','East'), ('role','URY Captain'), ('read',False)]:
            with self.subTest(change=change):
                self.doc.branch = value if change == 'branch' else 'West'
                self.f.get_roles.return_value = [value if change == 'role' else 'URY Cashier']
                self.f.has_permission.side_effect = lambda *a, **kw: change != 'read'
                with self.assertRaises(PermissionError): search(branch=self.doc.branch)
                with self.assertRaises(PermissionError): copy('INV-1', idempotency_key='one')
        self.f.has_permission.side_effect = None
        self.f.has_permission.return_value = True
        for self.doc.docstatus, self.doc.status in [(0,'Draft'), (2,'Cancelled')]:
            with self.assertRaises((ValueError, PermissionError)): copy('INV-1', idempotency_key='one')
