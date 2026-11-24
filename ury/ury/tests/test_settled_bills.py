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
        self.assertEqual(
            set(filters['status'][1]),
            {'Paid', 'Consolidated', 'Return', 'Partly Paid', 'Unpaid', 'Overdue', 'Credit Note Issued'},
        )
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

    def test_cashier_search_settled_bills_allows_same_till_return_and_skips_inaccessible_rows(self):
        """Finding 6c: a cashier searching settled bills on their own till never gets 403 when another cashier's return exists."""
        self.f.session = frappe._dict(user='cashier1@example.test')
        own_bill = frappe._dict(
            name='INV-OWN', branch='West', docstatus=1, status='Credit Note Issued',
            pos_profile='Till-UGX', restaurant_table='1', cashier='cashier1@example.test',
            waiter='cashier1@example.test', owner='cashier1@example.test', outstanding_amount=0,
            items=[dict(item_name='Burger', qty=1, amount=10000)], taxes=[],
            payments=[dict(mode_of_payment='Cash', amount=10000)],
        )
        other_cashier_return = frappe._dict(
            name='INV-RET-OTHER', branch='West', docstatus=1, status='Return', is_return=1,
            return_against='INV-OWN', pos_profile='Till-UGX', restaurant_table=None,
            cashier='cashier2@example.test', waiter='cashier2@example.test', owner='cashier2@example.test',
            outstanding_amount=0, items=[dict(item_name='Burger', qty=-1, amount=-10000)], taxes=[],
            payments=[dict(mode_of_payment='Cash', amount=-10000)],
        )
        other_till_bill = frappe._dict(
            name='INV-OTHER-TILL', branch='West', docstatus=1, status='Paid',
            pos_profile='Till-KWD', restaurant_table='9', cashier='cashier2@example.test',
            waiter='cashier2@example.test', owner='cashier2@example.test', outstanding_amount=0,
            items=[dict(item_name='Steak', qty=1, amount=25)], taxes=[],
            payments=[dict(mode_of_payment='Cash', amount=25)],
        )
        docs = {d.name: d for d in (own_bill, other_cashier_return, other_till_bill)}
        self.f.get_list.return_value = [frappe._dict(name=n) for n in docs]
        self.f.get_doc.side_effect = lambda dt, name, **kw: docs[name]
        self.f.db.exists.side_effect = lambda dt, filt=None: (
            'PPU-1'
            if dt == 'POS Profile User'
            and isinstance(filt, dict)
            and filt.get('parent') == 'Till-UGX'
            and filt.get('user') == 'cashier1@example.test'
            else None
        )

        def _strict_enforce(doc, require_modify=False):
            if doc.waiter != 'cashier1@example.test':
                raise PermissionError('Not permitted to access this order')

        with patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access', side_effect=_strict_enforce):
            rows = self.endpoint('search_settled_bills')(branch='West')

        returned_names = [r['name'] for r in rows]
        self.assertIn('INV-OWN', returned_names)
        self.assertIn('INV-RET-OTHER', returned_names)
        self.assertNotIn('INV-OTHER-TILL', returned_names)
