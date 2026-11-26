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

    def test_print_bill_copy_does_not_require_cups_for_browser_or_qz_print(self):
        records = []
        doc = frappe._dict(
            name='INV-B1', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[frappe._dict(item_name='Tea', qty=1, amount=5000)],
            taxes=[], payments=[frappe._dict(mode_of_payment='Cash', amount=5000)],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            self.assertEqual(arg['doctype'], 'URY Bill Reprint Log')
            rec = frappe._dict(arg)
            return type('Row', (), {'insert': lambda _, **kw: records.append(rec) or rec})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log' and isinstance(flt, dict):
                if 'request_key' in flt:
                    matches = [r for r in records if r.get('request_key') == flt['request_key']]
                    return matches[-1] if matches else None
                if flt.get('attempt') == 1 and fieldname == 'copy_number':
                    self.assertTrue(for_update, 'copy_number counter lookup must use a locking read (for_update=True)')
                    self.assertEqual(order_by, 'copy_number desc')
                    nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                    return max(nums) if nums else None
            return None

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=[]):
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'test-cashier'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = get_value
            f.db.count.side_effect = AssertionError('print_bill_copy must not use non-locking frappe.db.count for copy numbering')
            f.utils.now.return_value = '2026-11-23 12:34:00'
            f.get_print.side_effect = lambda dt, name, print_format=None, doc=None, **kw: (
                f'<article><h1>COPY #{doc.copy_number}</h1><p>{name}</p></article>'
            )

            browser = api.print_bill_copy('INV-B1', idempotency_key='browser-key', delivery='browser')
            qz = api.print_bill_copy('INV-B1', idempotency_key='qz-key', delivery='qz')

        self.assertEqual(browser['status'], 'RESERVED')
        self.assertEqual(browser['copy_number'], 1)
        self.assertEqual(browser['delivery'], 'browser')
        self.assertIn('COPY #1', browser['html'])
        self.assertIn('copy_number=1', browser['print_url'])
        self.assertEqual(qz['status'], 'RESERVED')
        self.assertEqual(qz['copy_number'], 2)
        self.assertEqual(qz['delivery'], 'qz')
        self.assertIn('COPY #2', qz['html'])
        self.assertEqual(len(records), 2)
        self.assertIsNone(records[0]['printer'])
        self.assertEqual(records[0]['copy_number'], 1)
        self.assertEqual(records[1]['copy_number'], 2)

    def test_printview_populates_copy_number_and_renders_hash_n(self):
        from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice
        from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice

        doc = object.__new__(CustomPOSInvoice)
        doc.__dict__.update(name='POS-INV-00024', docstatus=1, customer='Walk In', currency='UGX', outstanding_amount=0)
        with patch('ury.ury.class_overrides.pos_invoice.frappe') as f, \
             patch.object(POSInvoice, 'before_print'), \
             patch('ury.ury.doctype.ury_order.ury_order.check_split_target_access'):
            f.db.exists.return_value = True
            f.get_doc.return_value = frappe._dict(name='POS-INV-00024', docstatus=1, custom_merged_pos_invoice=None)
            f.form_dict = {}
            f.db.get_value.return_value = 3
            CustomPOSInvoice.before_print(doc)
        self.assertEqual(doc.copy_number, 3)

        path = Path(__file__).resolve().parents[1] / 'print_format/ury_bill_copy/ury_bill_copy.json'
        copy_html = json.loads(path.read_text())['html']
        # Direct /printview without in-memory copy_number still prints COPY #n
        pv_doc = frappe._dict(
            name='POS-INV-00024', docstatus=1, currency='UGX', items=[], taxes=[], payments=[],
            grand_total=100, rounded_total=100, outstanding_amount=0, copy_timestamp='2026-11-23 12:34',
        )
        fake_f = MagicMock()
        fake_f.db.get_value.return_value = None
        html_default = Environment(autoescape=False).from_string(copy_html).render(doc=pv_doc, frappe=fake_f, _=lambda x: x)
        self.assertIn('COPY #1', html_default)
        fake_f.db.get_value.return_value = 4
        html_logged = Environment(autoescape=False).from_string(copy_html).render(doc=pv_doc, frappe=fake_f, _=lambda x: x)
        self.assertIn('COPY #4', html_logged)

    def test_bill_copy_includes_merged_partner_items_payments_and_efris(self):
        """Reproduces Hydrogen's billcopy-merged live probe (POS-INV-00031 + partner POS-INV-00030)."""
        path = Path(__file__).resolve().parents[1] / 'print_format/ury_bill_copy/ury_bill_copy.json'
        partner = frappe._dict(
            name='POS-INV-00030', docstatus=1, currency='UGX', customer='Walk In',
            items=[frappe._dict(item_name='Water', qty=1, amount=10000)],
            taxes=[frappe._dict(description='Exempt', tax_amount=0)],
            payments=[frappe._dict(mode_of_payment='Cash', amount=10000)],
            grand_total=10000, rounded_total=10000, outstanding_amount=0,
            efris_e_invoice='EINV-30', tax_id=None,
        )
        primary = frappe._dict(
            name='POS-INV-00031', docstatus=1, currency='UGX', customer='Walk In',
            custom_merged_pos_invoice='POS-INV-00030', ury_print_partner=partner,
            items=[frappe._dict(item_name='Burger', qty=1, amount=15000)],
            taxes=[frappe._dict(description='VAT 18%', tax_amount=2288)],
            payments=[frappe._dict(mode_of_payment='Card', amount=15000)],
            grand_total=15000, rounded_total=15000, outstanding_amount=0,
            efris_e_invoice='EINV-31', tax_id=None, copy_number=1, copy_timestamp='2026-11-23 12:34',
        )
        e_invoices = {
            'EINV-31': frappe._dict(taxes=[frappe._dict(tax_category_code='01:A: Standard (18%)', net_amount=12712, tax_amount=2288)]),
            'EINV-30': frappe._dict(taxes=[frappe._dict(tax_category_code='03:C: Exempt', net_amount=10000, tax_amount=0)]),
        }
        efris_map = {
            'POS-INV-00031': {'state': 'fiscalised', 'fdn': '326041234567', 'verification_code': 'VC-31', 'qr': 'data:image/png;base64,AAA', 'sandbox': 0, 'is_return': 0},
            'POS-INV-00030': {'state': 'fiscalised', 'fdn': '326041234568', 'verification_code': 'VC-30', 'qr': 'data:image/png;base64,BBB', 'sandbox': 0, 'is_return': 0},
        }
        fake_f = MagicMock()
        fake_f.get_doc.side_effect = lambda dt, name: e_invoices[name]
        fake_f.utils.fmt_money.side_effect = lambda val, **kw: f'UGX {val}'
        html = Environment(autoescape=False).from_string(json.loads(path.read_text())['html']).render(
            doc=primary, frappe=fake_f, _=lambda x: x, efris_receipt_data=lambda b: efris_map[b.name],
        )
        for token in ('COPY #1', 'POS-INV-00031', 'Burger', 'Card', '326041234567', 'A: Standard (18%)',
                      'POS-INV-00030', 'Water', 'Cash', '326041234568', 'C: Exempt', '25000'):
            self.assertIn(token, html, token)

    def test_before_print_and_bill_copy_query_pass_frappe_v16_select_validation(self):
        """P0: Frappe v16 rejects SQL function strings like max(copy_number) in get_value."""
        from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice
        from frappe.database.query import _validate_select_field
        from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice

        def _raise_frappe_throw(msg, exc=frappe.ValidationError, **kw):
            raise exc(msg)

        seen_before_print = []
        doc = object.__new__(CustomPOSInvoice)
        doc.__dict__.update(name='POS-INV-00024', docstatus=1, customer='Walk In', currency='UGX', outstanding_amount=0)
        with patch('ury.ury.class_overrides.pos_invoice.frappe') as f, \
             patch.object(POSInvoice, 'before_print'), \
             patch('ury.ury.doctype.ury_order.ury_order.check_split_target_access'), \
             patch('frappe.database.query._', lambda msg: msg), \
             patch('frappe.database.query.frappe.throw', side_effect=_raise_frappe_throw):
            f.db.exists.return_value = True
            f.get_doc.return_value = frappe._dict(name='POS-INV-00024', docstatus=1, custom_merged_pos_invoice=None)
            f.form_dict = {}

            def capture_bp(doctype, flt, fieldname=None, **kw):
                seen_before_print.append((doctype, flt, fieldname, kw))
                _validate_select_field(fieldname)
                return 3

            f.db.get_value.side_effect = capture_bp
            CustomPOSInvoice.before_print(doc)

        self.assertEqual(doc.copy_number, 3)
        self.assertEqual(len(seen_before_print), 1)
        self.assertEqual(seen_before_print[0][2], 'copy_number')
        self.assertEqual(seen_before_print[0][3].get('order_by'), 'copy_number desc')

        # Now validate URY Bill Copy template's fallback query through Frappe v16's real validator too.
        path = Path(__file__).resolve().parents[1] / 'print_format/ury_bill_copy/ury_bill_copy.json'
        copy_html = json.loads(path.read_text())['html']
        pv_doc = frappe._dict(
            name='POS-INV-00024', docstatus=1, currency='UGX', items=[], taxes=[], payments=[],
            grand_total=100, rounded_total=100, outstanding_amount=0, copy_timestamp='2026-11-24 00:30',
        )
        seen_template = []
        with patch('frappe.database.query._', lambda msg: msg), \
             patch('frappe.database.query.frappe.throw', side_effect=_raise_frappe_throw):
            def capture_tpl(doctype, flt, fieldname=None, **kw):
                seen_template.append((doctype, flt, fieldname, kw))
                _validate_select_field(fieldname)
                return 4

            fake_f = MagicMock()
            fake_f.db.get_value.side_effect = capture_tpl
            html = Environment(autoescape=False).from_string(copy_html).render(doc=pv_doc, frappe=fake_f, _=lambda x: x)
        self.assertIn('COPY #4', html)
        self.assertEqual(len(seen_template), 1)
        self.assertEqual(seen_template[0][2], 'copy_number')
        self.assertEqual(seen_template[0][3].get('order_by'), 'copy_number desc')

    def test_reserved_replay_returns_copy_number_delivery_html_and_print_url(self):
        """P1: Replaying a reserved copy must return copy_number, delivery, html, and print_url."""
        records = []
        doc = frappe._dict(
            name='INV-R1', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)
            return type('Row', (), {'insert': lambda _, **kw: records.append(rec) or rec})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'POS Profile' and fieldname == 'qz_print':
                return 0
            if dt == 'URY Bill Reprint Log' and isinstance(flt, dict):
                if 'request_key' in flt:
                    matches = [r for r in records if r.get('request_key') == flt['request_key']]
                    return matches[-1] if matches else None
                if flt.get('attempt') == 1 and fieldname == 'copy_number':
                    nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                    return max(nums) if nums else None
            return None

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=[]):
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'test-cashier'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = get_value
            f.db.count.return_value = 0
            f.utils.now.return_value = '2026-11-24 00:30:00'
            f.get_print.side_effect = lambda dt, name, print_format=None, doc=None, **kw: (
                f'<article><h1>COPY #{doc.copy_number}</h1></article>'
            )

            first = api.print_bill_copy('INV-R1', idempotency_key='idem-1', delivery='browser')
            replay = api.print_bill_copy('INV-R1', idempotency_key='idem-1')

        self.assertEqual(len(records), 1)
        self.assertEqual(first['copy_number'], 1)
        self.assertEqual(replay['status'], 'RESERVED')
        self.assertEqual(replay.get('copy_number'), 1)
        self.assertEqual(replay.get('delivery'), 'browser')
        self.assertIn('COPY #1', replay.get('html') or '')
        self.assertIn('copy_number=1', replay.get('print_url') or '')

    def test_print_bill_copy_unique_copy_slot_and_retry_on_snapshot_race(self):
        """P1: URY Bill Reprint Log carries unique copy_slot and print_bill_copy retries on snapshot/duplicate race."""
        schema_path = Path(__file__).resolve().parents[1] / 'doctype/ury_bill_reprint_log/ury_bill_reprint_log.json'
        schema = json.loads(schema_path.read_text())
        fields = {f['fieldname']: f for f in schema['fields']}
        self.assertIn('copy_slot', fields, 'URY Bill Reprint Log must declare a unique copy_slot field')
        self.assertEqual(fields['copy_slot'].get('unique'), 1)

        records = []
        doc = frappe._dict(
            name='INV-RACE', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )
        attempts = {'count': 0}

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)

            def insert(_self, **kw):
                attempts['count'] += 1
                if attempts['count'] == 1:
                    # Simulate a racing connection having just committed COPY #1 on the same slot.
                    records.append(frappe._dict(
                        name='COPY-other-1', print_job_id='COPY-other-1', request_key='other-key',
                        invoice='INV-RACE', copy_number=1, attempt=1, copy_slot='INV-RACE:1:1', status='COMPLETED',
                    ))
                    raise frappe.DuplicateEntryError('URY Bill Reprint Log', rec.get('copy_slot'))
                records.append(rec)
                return rec

            return type('Row', (), {'insert': insert})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log' and isinstance(flt, dict):
                if 'request_key' in flt:
                    matches = [r for r in records if r.get('request_key') == flt['request_key']]
                    return matches[-1] if matches else None
                if flt.get('attempt') == 1 and fieldname == 'copy_number':
                    self.assertTrue(for_update)
                    nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                    return max(nums) if nums else None
            return None

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=[]):
            f.DuplicateEntryError = frappe.DuplicateEntryError
            f.QueryDeadlockError = frappe.QueryDeadlockError
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'test-cashier'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = get_value
            f.utils.now.return_value = '2026-11-24 00:30:00'
            f.get_print.side_effect = lambda dt, name, print_format=None, doc=None, **kw: (
                f'<article><h1>COPY #{doc.copy_number}</h1></article>'
            )

            res = api.print_bill_copy('INV-RACE', idempotency_key='race-key', delivery='browser')

        self.assertEqual(res['status'], 'RESERVED')
        self.assertEqual(res['copy_number'], 2)
        self.assertEqual([r['copy_number'] for r in records], [1, 2])
        self.assertEqual(records[-1].get('copy_slot'), 'INV-RACE:2:1')
        f.db.rollback.assert_called()

    def test_reprint_dispatch_is_idempotent_across_users_and_marks_network_pending_dispatched(self):
        """P1: Network dispatch marks PENDING-EFRIS DISPATCHED; another user with same key does not print COPY #2."""
        records = []
        pending_status = {'PENDING-EFRIS-INV-NET1': 'AWAITING_FDN'}
        doc = frappe._dict(
            name='INV-NET1', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)
            return type('Row', (), {'insert': lambda _, **kw: records.append(rec) or rec})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log':
                if isinstance(flt, str):
                    return next((r for r in records if r.name == flt), None)
                if isinstance(flt, dict):
                    if 'request_key' in flt:
                        matches = [r for r in records if r.get('request_key') == flt['request_key']]
                        return matches[-1] if matches else None
                    if flt.get('attempt') == 1 and fieldname == 'copy_number':
                        nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                        return max(nums) if nums else None
            return None

        def set_value(dt, name, values, **kw):
            if dt == 'URY Bill Reprint Log':
                if name in pending_status:
                    pending_status[name] = values.get('status', pending_status[name])
                    return
                for r in records:
                    if r.name == name:
                        r.update(values)

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=['Bar Printer']), \
             patch('ury.ury.printing.file_store.save_job', return_value=True), \
             patch('ury.ury.printing.service.submit_and_monitor_print_job', return_value={'status': 'Success'}) as submit:
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = get_value
            f.db.set_value.side_effect = set_value
            f.db.exists.side_effect = lambda dt, name: dt == 'URY Bill Reprint Log' and name in pending_status
            f.utils.now.return_value = '2026-11-24 00:30:00'

            # 1) Cashier dispatches efris-auto-INV-NET1 over network
            f.session.user = 'cashier@example.com'
            cashier_res = api.print_bill_copy('INV-NET1', idempotency_key='efris-auto-INV-NET1', delivery='network')

            # 2) Scheduler (Administrator) attempts the same auto-reprint key
            f.session.user = 'Administrator'
            cron_res = api.print_bill_copy('INV-NET1', idempotency_key='efris-auto-INV-NET1', delivery='network')

        self.assertEqual(cashier_res['status'], 'SUBMITTED')
        self.assertEqual(pending_status['PENDING-EFRIS-INV-NET1'], 'DISPATCHED')
        self.assertEqual(submit.call_count, 1, 'Different session user must not dispatch a duplicate COPY #2')
        self.assertEqual(len(records), 1)
        self.assertEqual(cron_res['print_job_id'], cashier_res['print_job_id'])

    def test_print_bill_copy_retry_uses_savepoint_without_committing_or_wiping_caller_tx(self):
        """R4 P1: Retry uses a savepoint and never commits or bare-rolls-back a caller's transaction."""
        records = []
        doc = frappe._dict(
            name='INV-SP1', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )
        attempts = {'count': 0}

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)

            def insert(_self, **kw):
                attempts['count'] += 1
                if attempts['count'] == 1:
                    raise frappe.QueryDeadlockError('simulated 1213 deadlock')
                records.append(rec)
                return rec

            return type('Row', (), {'insert': insert})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log' and isinstance(flt, dict):
                if 'request_key' in flt:
                    matches = [r for r in records if r.get('request_key') == flt['request_key']]
                    return matches[-1] if matches else None
                if flt.get('attempt') == 1 and fieldname == 'copy_number':
                    nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                    return max(nums) if nums else None
            return None

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=[]):
            f.DuplicateEntryError = frappe.DuplicateEntryError
            f.QueryDeadlockError = frappe.QueryDeadlockError
            f.QueryTimeoutError = getattr(frappe, 'QueryTimeoutError', TimeoutError)
            f.db.transaction_writes = 1  # External caller has uncommitted work in this transaction.
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'test-cashier'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = get_value
            f.utils.now.return_value = '2026-11-24 06:30:00'
            f.get_print.side_effect = lambda dt, name, print_format=None, doc=None, **kw: (
                f'<article><h1>COPY #{doc.copy_number}</h1></article>'
            )

            res = api.print_bill_copy('INV-SP1', idempotency_key='sp-key', delivery='browser')

        self.assertEqual(res['status'], 'RESERVED')
        self.assertEqual(res['copy_number'], 1)
        f.db.savepoint.assert_called()
        f.db.rollback.assert_called()
        for call in f.db.rollback.call_args_list:
            self.assertTrue(
                call.kwargs.get('save_point'),
                f'print_bill_copy must roll back only its savepoint, not caller transaction: {call}',
            )
        f.db.commit.assert_not_called()

    def test_print_bill_copy_recognizes_legacy_r2_request_key_and_prevents_copy_2(self):
        """R4 P2: Pre-upgrade R2 [owner, invoice, key] row must not be duplicated as COPY #2."""
        from hashlib import sha256

        legacy_key = sha256(json.dumps(['cashier@example.com', 'INV-UPG1', 'efris-auto-INV-UPG1']).encode()).hexdigest()
        records = [
            frappe._dict(
                name=f'COPY-{legacy_key}-1',
                print_job_id=f'COPY-{legacy_key}-1',
                request_key=legacy_key,
                copy_slot=None,
                invoice='INV-UPG1',
                job_owner='cashier@example.com',
                printer='Bar Printer',
                status='SUBMITTED',
                failure_reason=None,
                copy_number=1,
                attempt=1,
            ),
        ]
        pending_status = {'PENDING-EFRIS-INV-UPG1': 'AWAITING_FDN'}
        doc = frappe._dict(
            name='INV-UPG1', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)
            return type('Row', (), {'insert': lambda _, **kw: records.append(rec) or rec})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log':
                if isinstance(flt, str):
                    if flt == 'PENDING-EFRIS-INV-UPG1' and fieldname == 'job_owner':
                        return 'cashier@example.com'
                    return next((r for r in records if r.name == flt), None)
                if isinstance(flt, dict):
                    if 'request_key' in flt:
                        matches = [r for r in records if r.get('request_key') == flt['request_key']]
                        return matches[-1] if matches else None
                    if flt.get('attempt') == 1 and fieldname == 'copy_number':
                        nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                        return max(nums) if nums else None
            return None

        def set_value(dt, name, values, **kw):
            if dt == 'URY Bill Reprint Log' and name in pending_status:
                pending_status[name] = values.get('status', pending_status[name])

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=['Bar Printer']), \
             patch('ury.ury.printing.file_store.save_job', return_value=True), \
             patch('ury.ury.printing.service.submit_and_monitor_print_job', return_value={'status': 'Success'}) as submit:
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'cashier@example.com'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.db.get_value.side_effect = get_value
            f.db.set_value.side_effect = set_value
            f.db.exists.side_effect = lambda dt, name: dt == 'URY Bill Reprint Log' and name in pending_status
            f.utils.now.return_value = '2026-11-24 06:30:00'

            res = api.print_bill_copy('INV-UPG1', idempotency_key='efris-auto-INV-UPG1', delivery='network')

        self.assertEqual(res['status'], 'SUBMITTED')
        self.assertEqual(res['copy_number'], 1)
        self.assertEqual(res['print_job_id'], f'COPY-{legacy_key}-1')
        submit.assert_not_called()
        self.assertEqual(len(records), 1)
        self.assertEqual(pending_status['PENDING-EFRIS-INV-UPG1'], 'DISPATCHED')

    def test_copy_race_errno_unwraps_frappe_wrapped_mariadb_errors(self):
        """R5 P1: _copy_race_errno must unwrap Frappe's QueryDeadlockError(OperationalError(1020, ...)) wrappers."""
        import pymysql

        op_1020 = pymysql.err.OperationalError(1020, "Record has changed since last read in table 'tabPOS Invoice'")
        wrapped_1020 = frappe.QueryDeadlockError(op_1020)
        self.assertEqual(api._copy_race_errno(wrapped_1020), 1020)

        try:
            try:
                raise op_1020
            except Exception as inner:
                raise frappe.QueryDeadlockError("deadlock") from inner
        except Exception as chained_1020:
            self.assertEqual(api._copy_race_errno(chained_1020), 1020)

        op_1213 = pymysql.err.OperationalError(1213, "Deadlock found when trying to get lock")
        self.assertEqual(api._copy_race_errno(frappe.QueryDeadlockError(op_1213)), 1213)

        op_1305 = pymysql.err.OperationalError(1305, "SAVEPOINT ury_print_bill_copy_0 does not exist")
        self.assertEqual(api._copy_race_errno(op_1305), 1305)

        dup_1062 = frappe.DuplicateEntryError(
            "URY Bill Reprint Log",
            "INV-1:1:1",
            pymysql.err.IntegrityError(1062, "Duplicate entry 'INV-1:1:1' for key 'copy_slot'"),
        )
        self.assertEqual(api._copy_race_errno(dup_1062), 1062)
        self.assertIsNone(api._copy_race_errno(ValueError("unrelated")))

    def test_print_bill_copy_isolated_db_recovers_1020_with_caller_writes_without_touching_caller_tx(self):
        """R5 P1: When caller has uncommitted writes, copy claiming runs on an isolated DB connection so 1020 retries never fail or touch caller tx."""
        import pymysql

        records = []
        doc = frappe._dict(
            name='INV-ISO1', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )
        attempts = {'n': 0}
        claim_events = []

        claim_db = MagicMock(name='claim_db')
        claim_db.cur_db_name = 'test_pos_db'
        claim_db.transaction_writes = 0

        def claim_sql(query, values=None, **kw):
            q = str(query)
            if 'GET_LOCK' in q:
                claim_events.append('get_lock')
                return [(1,)]
            if 'RELEASE_LOCK' in q:
                claim_events.append('release_lock')
                return [(1,)]
            return []

        claim_db.sql.side_effect = claim_sql
        claim_db.commit.side_effect = lambda: claim_events.append('claim_commit')
        claim_db.rollback.side_effect = lambda **kw: claim_events.append('claim_rollback')
        claim_db.close.side_effect = lambda: claim_events.append('claim_close')
        claim_db.exists.return_value = False

        def claim_get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log' and isinstance(flt, dict):
                if 'request_key' in flt:
                    matches = [r for r in records if r.get('request_key') == flt['request_key']]
                    return matches[-1] if matches else None
                if flt.get('attempt') == 1 and fieldname == 'copy_number':
                    self.assertTrue(for_update)
                    nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                    return max(nums) if nums else None
            return None

        claim_db.get_value.side_effect = claim_get_value

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)

            def insert(_self, **kw):
                attempts['n'] += 1
                if attempts['n'] == 1:
                    # Simulate racing session committing COPY #1 and MariaDB 11.8 raising wrapped 1020.
                    records.append(frappe._dict(
                        name='COPY-other-1', print_job_id='COPY-other-1', request_key='other-key',
                        invoice='INV-ISO1', copy_number=1, attempt=1, copy_slot='INV-ISO1:1:1', status='COMPLETED',
                    ))
                    raise frappe.QueryDeadlockError(
                        pymysql.err.OperationalError(1020, "Record has changed since last read in table 'tabURY Bill Reprint Log'")
                    )
                records.append(rec)
                return rec

            return type('Row', (), {'insert': insert})()

        caller_db = MagicMock(name='caller_db')
        caller_db.transaction_writes = 1  # Caller has uncommitted writes in its own transaction.
        caller_db.get_value.return_value = None
        caller_db.exists.return_value = False

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch.object(api, '_open_isolated_copy_db', return_value=claim_db, create=True) as opener, \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=[]):
            f.db = caller_db
            f.DuplicateEntryError = frappe.DuplicateEntryError
            f.QueryDeadlockError = frappe.QueryDeadlockError
            f.QueryTimeoutError = getattr(frappe, 'QueryTimeoutError', TimeoutError)
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'test-cashier'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.utils.now.return_value = '2026-11-24 09:00:00'
            f.get_print.side_effect = lambda dt, name, print_format=None, doc=None, **kw: (
                f'<article><h1>COPY #{doc.copy_number}</h1></article>'
            )

            res = api.print_bill_copy('INV-ISO1', idempotency_key='caller-writes-key', delivery='browser')

            self.assertIs(f.db, caller_db, 'Caller DB handle must be restored after isolated copy claim')

        opener.assert_called()
        self.assertEqual(res['status'], 'RESERVED')
        self.assertEqual(res['copy_number'], 2)
        self.assertEqual([r['copy_number'] for r in records], [1, 2])
        self.assertIn('get_lock', claim_events)
        self.assertIn('claim_rollback', claim_events)
        self.assertIn('claim_commit', claim_events)
        self.assertIn('release_lock', claim_events)
        self.assertIn('claim_close', claim_events)
        caller_db.commit.assert_not_called()
        caller_db.rollback.assert_not_called()

    def test_print_bill_copy_fallback_unwraps_1020_for_read_only_caller(self):
        """R5 P1: Even on the fallback path without an isolated connection, wrapped 1020 hits full rollback (not savepoint) for a read-only caller."""
        import pymysql

        records = []
        doc = frappe._dict(
            name='INV-RO1020', status='Paid', branch='West', pos_profile='Bar', docstatus=1,
            restaurant_table='T1', currency='UGX', items=[], taxes=[], payments=[],
            grand_total=5000, rounded_total=5000, outstanding_amount=0,
        )
        attempts = {'n': 0}

        def get_doc(arg, *args, **kwargs):
            if arg == 'POS Invoice':
                return frappe._dict(doc)
            rec = frappe._dict(arg)

            def insert(_self, **kw):
                attempts['n'] += 1
                if attempts['n'] == 1:
                    raise frappe.QueryDeadlockError(
                        pymysql.err.OperationalError(1020, "Record has changed since last read")
                    )
                records.append(rec)
                return rec

            return type('Row', (), {'insert': insert})()

        def get_value(dt, flt=None, fieldname=None, as_dict=False, order_by=None, for_update=False, **kw):
            if dt == 'URY Bill Reprint Log' and isinstance(flt, dict):
                if 'request_key' in flt:
                    matches = [r for r in records if r.get('request_key') == flt['request_key']]
                    return matches[-1] if matches else None
                if flt.get('attempt') == 1 and fieldname == 'copy_number':
                    nums = [r['copy_number'] for r in records if r.get('invoice') == flt.get('invoice') and r.get('attempt') == 1]
                    return max(nums) if nums else None
            return None

        with patch.object(api, 'frappe') as f, patch.object(api, 'getBranch', return_value='West'), \
             patch('ury.ury.doctype.ury_order.ury_order._enforce_order_access'), \
             patch('ury.ury.api.ury_print._bill_printers', return_value=[]):
            f.DuplicateEntryError = frappe.DuplicateEntryError
            f.QueryDeadlockError = frappe.QueryDeadlockError
            f.QueryTimeoutError = getattr(frappe, 'QueryTimeoutError', TimeoutError)
            f.db.transaction_writes = 0
            f.db.get_value.side_effect = get_value
            f.get_doc.side_effect = get_doc
            f.get_roles.return_value = ['URY Cashier']
            f.session.user = 'test-cashier'
            f.has_permission.side_effect = lambda dt, perm, **kw: perm == 'read'
            f.utils.now.return_value = '2026-11-24 09:00:00'
            f.get_print.side_effect = lambda dt, name, print_format=None, doc=None, **kw: (
                f'<article><h1>COPY #{doc.copy_number}</h1></article>'
            )

            res = api.print_bill_copy('INV-RO1020', idempotency_key='ro-1020-key', delivery='browser')

        self.assertEqual(res['status'], 'RESERVED')
        self.assertEqual(res['copy_number'], 1)
        f.db.rollback.assert_called_once_with()

    def test_format_bill_money_respects_currency_format_and_fraction_units_on_multi_currency_site(self):
        """R5 P2: format_bill_money formats UGX whole shillings and USD cents simultaneously on the same site."""
        from ury import hooks
        from ury.ury.print_format.utils import format_bill_money
        from frappe.utils.number_format import NumberFormat

        self.assertIn('ury.ury.print_format.utils.format_bill_money', hooks.jinja.get('methods', []))

        def run_with_currencies(currencies, *, sys_currency_precision, site_number_format='#,###.##'):
            def fake_get_value(doctype, name, fieldname=None, as_dict=False, cache=False, **kw):
                if doctype != 'Currency':
                    return None
                info = currencies.get(name)
                if not info:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    data = {k: info.get(k) for k in fieldname}
                    return frappe._dict(data) if as_dict else tuple(data[k] for k in fieldname)
                return info.get(fieldname)

            fake_db = MagicMock()
            fake_db.get_value.side_effect = fake_get_value
            with patch('frappe.utils.data.frappe.db', fake_db), \
                 patch('ury.ury.print_format.utils.frappe.db', fake_db), \
                 patch('frappe.utils.data.get_currency_precision', return_value=sys_currency_precision), \
                 patch('frappe.utils.data.get_number_format', return_value=NumberFormat.from_string(site_number_format)), \
                 patch('frappe.locale.get_number_format', return_value=NumberFormat.from_string(site_number_format)), \
                 patch('frappe.utils.data.frappe.defaults.get_global_default', return_value=None), \
                 patch('frappe.utils.data.frappe._', side_effect=lambda s: s):
                return {
                    'ugx_35000': format_bill_money(35000.0, 'UGX'),
                    'ugx_vat': format_bill_money(5338.98, 'UGX'),
                    'ugx_zero': format_bill_money(0.0, 'UGX'),
                    'usd_1250': format_bill_money(12.50, 'USD'),
                    'usd_vat': format_bill_money(1.91, 'USD'),
                    'usd_net': format_bill_money(10.59, 'USD'),
                    'usd_zero': format_bill_money(0.0, 'USD'),
                    'omr_12500': format_bill_money(12.5, 'OMR'),
                }

        # Case 1: Currency.number_format configured (UGX whole shillings, USD cents, OMR 3 decimals)
        # even when System Settings.currency_precision is pinned to 0 for UGX!
        out_by_format = run_with_currencies(
            {
                'UGX': {'number_format': '#,###', 'fraction_units': 1, 'symbol': 'UGX', 'symbol_on_right': 0},
                'USD': {'number_format': '#,###.##', 'fraction_units': 100, 'symbol': '$', 'symbol_on_right': 0},
                'OMR': {'number_format': '#,###.###', 'fraction_units': 1000, 'symbol': 'OMR', 'symbol_on_right': 0},
            },
            sys_currency_precision=0,
        )
        self.assertEqual(out_by_format['ugx_35000'], 'UGX 35,000')
        self.assertEqual(out_by_format['ugx_vat'], 'UGX 5,339')
        self.assertEqual(out_by_format['ugx_zero'], 'UGX 0')
        self.assertEqual(out_by_format['usd_1250'], '$ 12.50')
        self.assertEqual(out_by_format['usd_vat'], '$ 1.91')
        self.assertEqual(out_by_format['usd_net'], '$ 10.59')
        self.assertEqual(out_by_format['usd_zero'], '$ 0.00')
        self.assertEqual(out_by_format['omr_12500'], 'OMR 12.500')

        # Case 2: Currency.number_format blank -> derives precision from Currency.fraction_units
        out_by_fraction = run_with_currencies(
            {
                'UGX': {'number_format': '', 'fraction_units': 1, 'symbol': 'UGX', 'symbol_on_right': 0},
                'USD': {'number_format': '', 'fraction_units': 100, 'symbol': '$', 'symbol_on_right': 0},
                'OMR': {'number_format': '', 'fraction_units': 1000, 'symbol': 'OMR', 'symbol_on_right': 0},
            },
            sys_currency_precision=None,
        )
        self.assertEqual(out_by_fraction['ugx_35000'], 'UGX 35,000')
        self.assertEqual(out_by_fraction['usd_1250'], '$ 12.50')
        self.assertEqual(out_by_fraction['omr_12500'], 'OMR 12.500')
