"""COPY dispatch must leave every business record untouched on every outcome."""
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jinja2 import Environment
from ury.ury.printing import finalizer
from ury.ury.doctype.ury_print_job import ury_print_job as virtual
from ury.ury_pos import api
import frappe


class BillCopy(unittest.TestCase):
    def test_terminal_states_never_write_to_business_records(self):
        meta = dict(job_type='BILL_COPY', invoice='INV-1', restaurant_table='T1', job_owner='cashier')
        with patch.object(finalizer, '_set_finalized_idempotency_key', return_value=True), \
             patch.object(finalizer, 'get_print_job', return_value=meta), \
             patch.object(finalizer, 'update_print_job'), patch.object(finalizer, 'notify_print_failure'), \
             patch.object(finalizer, '_free_tables_if_no_open_invoices') as free, patch.object(finalizer, 'frappe') as f:
            for state in ('COMPLETED', 'FAILED', 'UNKNOWN', 'CANCELED'):
                with self.subTest(state=state):
                    result = finalizer.finalize_print_job('COPY-test-1', state)
                    self.assertEqual(result.get('job_type'), 'BILL_COPY')
                    free.assert_not_called()
                    f.db.set_value.assert_called_with('URY Bill Reprint Log', 'COPY-test-1',
                        {'status': state, 'failure_reason': None})
                    for call in f.db.set_value.call_args_list:
                        self.assertEqual(call.args[0], 'URY Bill Reprint Log')
                    f.get_doc.assert_not_called()

    def test_desk_cannot_retype_or_change_copy_job(self):
        with patch.object(virtual, 'get_job', return_value=dict(job_type='BILL_COPY')), \
             patch.object(virtual, 'save_job') as save, patch.object(virtual, 'frappe') as f:
            f.PermissionError = PermissionError
            f.throw.side_effect = lambda message, exc: (_ for _ in ()).throw(exc(message))
            doc = MagicMock(name='job'); doc.as_dict.return_value = dict(job_type='BILL', status='COMPLETED')
            with self.assertRaisesRegex(PermissionError, r'^\[POS-COPY-001\] '): virtual.URYPrintJob.db_update(doc)
            save.assert_not_called()

    def test_copy_refusals_show_a_neutral_code(self):
        from ury.ury.doctype.ury_bill_reprint_log import ury_bill_reprint_log as reprint_log
        from ury.ury.printing import api as printing
        def raise_it(message, exc):
            raise exc(message)

        doc = MagicMock(name='log')
        doc.is_new.return_value = False
        with patch.object(reprint_log, 'frappe') as f:
            f.PermissionError = PermissionError
            f.throw.side_effect = raise_it
            for event in (reprint_log.URYBillReprintLog.validate, reprint_log.URYBillReprintLog.on_trash):
                with self.subTest(event=event.__name__), self.assertRaisesRegex(PermissionError, r'^\[POS-COPY-001\] '):
                    event(doc)
        with patch.object(printing, 'get_job', return_value=dict(job_type='BILL_COPY')), \
             patch.object(printing, 'frappe') as f, patch.object(printing, '_', side_effect=lambda message: message):
            f.PermissionError = PermissionError
            f.throw.side_effect = raise_it
            with self.assertRaisesRegex(PermissionError, r'^\[POS-COPY-001\] Retry from the closed check\.$'):
                printing.retry_print_job('COPY-test-1')

    def test_native_format_renders_original_details(self):
        path = Path(__file__).resolve().parents[1] / 'print_format/ury_bill_copy/ury_bill_copy.json'
        self.assertTrue(path.exists(), 'Missing native COPY format')
        spec = json.loads(path.read_text()); self.assertEqual(spec['print_format_type'], 'Jinja')
        doc = frappe._dict(name='INV-22', currency='UGX', items=[frappe._dict(item_name='Rice', qty=3, amount=30)],
                           taxes=[frappe._dict(description='VAT', tax_amount=4)], payments=[frappe._dict(mode_of_payment='Visa', amount=34)],
                           grand_total=34, rounded_total=34, outstanding_amount=0, copy_number=2, copy_timestamp='2026-11-21 12:34')
        html = Environment(autoescape=True).from_string(spec['html']).render(doc=doc, _=lambda x:x)
        for value in ('COPY','INV-22','Rice','VAT','Visa','34','2026-11-21 12:34'): self.assertIn(value, html)

    def test_original_receipt_includes_account_reference_in_print_only_terms(self):
        from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice
        from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
        doc = object.__new__(CustomPOSInvoice)
        doc.__dict__.update(name='INV-22', terms='Welcome', customer='AC-012', currency='UGX', outstanding_amount=60)
        with patch('ury.ury.class_overrides.pos_invoice.frappe') as f, patch.object(POSInvoice, 'before_print'), patch('ury.ury.class_overrides.pos_invoice._', lambda x:x):
            f.db.exists.return_value = False
            CustomPOSInvoice.before_print(doc)
        self.assertIn('Charged to account AC-012: 60 UGX', doc.terms)
        self.assertIn('Welcome', doc.terms)
        f.db.set_value.assert_not_called()

    def test_copy_shows_which_account_owes_the_remaining_balance(self):
        path = Path(__file__).resolve().parents[1] / 'print_format/ury_bill_copy/ury_bill_copy.json'
        doc = frappe._dict(customer='AC-012', currency='UGX', items=[], payments=[frappe._dict(mode_of_payment='Visa',amount=40)],
                           outstanding_amount=60, taxes=[], copy_timestamp='today')
        html = Environment(autoescape=True).from_string(json.loads(path.read_text())['html']).render(doc=doc, _=lambda x:x)
        self.assertIn('Charged to account AC-012: 60 UGX',html); self.assertIn('Visa: 40 UGX',html)

    def test_fully_on_account_copy_omits_zero_cash(self):
        path = Path(__file__).resolve().parents[1] / 'print_format/ury_bill_copy/ury_bill_copy.json'
        doc = frappe._dict(customer='AC-012', currency='UGX', items=[], taxes=[], copy_timestamp='today',
                           outstanding_amount=100, payments=[frappe._dict(mode_of_payment='Cash', amount=0)])
        html = Environment(autoescape=True).from_string(json.loads(path.read_text())['html']).render(doc=doc, _=lambda x:x)
        self.assertIn('Charged to account AC-012: 100 UGX', html)
        self.assertNotIn('Cash:', html)

    def test_durable_attempts_survive_spool_expiry_and_replay(self):
        fn = getattr(api, 'print_bill_copy', None)
        self.assertTrue(callable(fn), 'Missing audited copy dispatcher')
        records = []
        doc = frappe._dict(name='INV-1', status='Partly Paid', branch='West', pos_profile='Bar', docstatus=1)
        original = dict(doc)
        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice': return frappe._dict(doc)
            self.assertEqual(arg['doctype'], 'URY Bill Reprint Log')
            record = frappe._dict(arg)
            return type('Pending', (), {'insert': lambda _, **kw: records.append(record) or record})()
        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=['Bar printer']), \
             patch('ury.ury.printing.service.submit_and_monitor_print_job') as submit, \
             patch('ury.ury.printing.file_store.save_job', return_value=True):
            f.get_doc.side_effect = get_doc; f.get_roles.return_value = ['URY Cashier']; f.session.user='test-cashier'
            f.get_all.return_value = ['Bar printer']; f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = lambda dt,*a,**kw: records[-1] if dt=='URY Bill Reprint Log' and records else None
            def update(dt, name, fields, **kw):
                self.assertEqual(dt, 'URY Bill Reprint Log'); next(r for r in records if r.name==name).update(fields)
            f.db.set_value.side_effect = update; f.db.count.return_value=0
            submit.side_effect = [dict(status='Failure',message='offline'), dict(status='Success')]
            first = fn('INV-1', idempotency_key='same'); second = fn('INV-1', idempotency_key='same')
            third = fn('INV-1', idempotency_key='same')
            self.assertEqual(first['status'], 'FAILED'); self.assertEqual(second, third)
            self.assertEqual(submit.call_count, 2); self.assertEqual(len(records), 2)
            self.assertEqual(records[1].retry_of, records[0].name)
            self.assertEqual(records[0].failure_reason, 'offline'); self.assertEqual(records[1].job_owner,'test-cashier')
            for c in submit.call_args_list:
                self.assertEqual(c.kwargs['job_type'],'BILL_COPY'); self.assertEqual(c.kwargs['print_format'],'URY Bill Copy')
            self.assertEqual(original, dict(doc)); f.db.sql.assert_not_called()
            self.assertFalse(any(c.args[0] in ('Sales Invoice','Payment Entry','URY KOT') for c in f.get_doc.call_args_list if isinstance(c.args[0], str)))

    def test_transport_tracks_copy_even_when_profile_disables_tracking(self):
        import sys
        import tempfile
        from ury.ury.printing import service
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, 'frappe') as f, \
             patch.object(service,'validate_print_permission'), patch.object(service,'is_print_status_tracking_disabled',return_value=True), \
             patch.object(service, 'register_print_job',return_value='COPY-example') as register, \
             patch.dict(sys.modules, cups=MagicMock()) as modules:
            f.generate_hash.return_value = Path(tmp).name
            doc = frappe._dict(name='INV-1', pos_profile='Bar'); f.get_print.return_value=b'PDF'
            result = service.submit_and_monitor_print_job('POS Invoice','INV-1','Printer','URY Bill Copy', doc=doc,
                job_type='BILL_COPY', extra_metadata={'print_job_id':'COPY-example','invoice':'INV-1'})
            self.assertEqual(result['status'], 'Success'); self.assertEqual(result['print_job_id'], 'COPY-example')
            self.assertEqual(register.call_args.args[0]['job_type'],'BILL_COPY')
            self.assertEqual(modules['cups'].Connection().printFile.call_args.args[2], 'COPY-example')

    def test_only_internal_copy_render_uses_read_on_saved_split(self):
        from ury.ury.class_overrides import pos_invoice
        module = 'ury.ury.doctype.ury_order.ury_order'
        doc = MagicMock(spec=pos_invoice.CustomPOSInvoice)
        doc.name = 'INV-split'
        with patch.object(pos_invoice, 'frappe') as f, patch.object(pos_invoice.POSInvoice,'before_print'), \
             patch(module + '.check_split_target_access') as check:
            for marker, permission in [(True,'write'), (getattr(pos_invoice, '_COPY_RENDER_MARKER', object()),'read')]:
                doc._ury_copy_render = marker
                pos_invoice.CustomPOSInvoice.before_print(doc)
                check.assert_called_with(f.get_doc.return_value, permission)

    def test_copy_identity_with_missing_metadata_never_falls_back_to_bill(self):
        with patch.object(finalizer, '_set_finalized_idempotency_key', return_value=True), \
             patch.object(finalizer, 'get_print_job', return_value={'invoice': 'INV-1', 'restaurant_table': 'T1'}), \
             patch.object(finalizer, 'update_print_job'), patch.object(finalizer,'frappe') as f, \
             patch.object(finalizer,'_free_tables_if_no_open_invoices') as free:
            finalizer.finalize_print_job('COPY-' + '1'*64 + '-1', 'COMPLETED')
            free.assert_not_called()
            self.assertFalse(any(c.args[0] == 'POS Invoice' for c in f.db.set_value.call_args_list))

    def test_merged_receipt_includes_cash_and_remaining_credit_on_partner(self):
        form = Path(__file__).resolve().parents[1] / 'print_format/merged_pos_invoice_format/merged_pos_invoice_format.json'
        doc = MagicMock(customer='AC-12', currency='UGX', outstanding_amount=0, paid_amount=100, change_amount=0,
                        custom_merged_pos_invoice='B2', grand_total=100, custom_merged_total=80,
                        payments=[MagicMock(mode_of_payment='Cash', amount=100)])
        partner = MagicMock(customer='AC-12', currency='UGX', outstanding_amount=60, paid_amount=20,
                            payments=[MagicMock(mode_of_payment='Visa', amount=20)])
        doc.ury_print_partner = partner
        html = Environment().from_string(json.loads(form.read_text())['html']).render(doc=doc, frappe=MagicMock(), _=lambda x:x)
        for value in ('Cash', 'Visa', 'Charged to account AC-12: 60 UGX'):
            self.assertIn(value, html)
