"""Cross-check kitchen release and reservation identities on native, rollback-only orders."""
from unittest import TestCase
from unittest.mock import patch

import frappe

from ury.tests.offline_pos_utils import make_made_to_order_item, make_offline_pos, send_order
from ury.ury.api import self_ordering, ury_kot_validation
from ury.ury.api.ury_fulfilment_posting_service import _reservation_rows, _row_reservation_line_key
from ury.ury.api.ury_kot_generate import kot_execute
from ury.ury.api.ury_pos_invoice_qty_reduction import reduce_order_item_qty
from ury.ury.doctype.ury_order.ury_order import fire_items, split_bill


class TestKitchenReleaseIntegrity(TestCase):
    def setUp(self):
        self.addCleanup(frappe.set_user, 'Administrator')
        self.addCleanup(frappe.db.rollback)
        self.c = make_offline_pos()
        frappe.db.set_value('POS Profile', self.c.profile.name, {
            'ury_enable_hold_fire': 1, 'custom_qty_reduction_allowed_order_types': 'Take Away',
        })

    def order(self, *lines):
        rows = [dict(item=item.name, item_name=item.item_name, qty=1, rate=1,
                     reservation_line_key=key, ury_held=int(held), comment=key) for item, key, held in lines]
        return frappe.get_doc('POS Invoice', send_order(self.c, items=rows).name)

    def tickets(self, invoice):
        return frappe.get_all('URY KOT', filters={'invoice': invoice, 'docstatus': 1}, pluck='name')

    def assert_reserved(self, ticket, dish):
        line, = ticket.kot_items
        rows = _reservation_rows(ticket.invoice, dish.name, self.c.branch.name,
                                 self.c.restaurant.company, line.reservation_line_key)
        self.assertEqual({_row_reservation_line_key(row) for row in rows}, {line.reservation_line_key})

    def test_scheduler_then_fire_then_retry(self):
        invoice = self.order((self.c.item, 'seat-1', True))
        # Observe fallback dispatch before the printer/submit boundary as well as persisted tickets.
        with patch.object(ury_kot_validation, 'create_kot', wraps=ury_kot_validation.create_kot) as fallback:
            ury_kot_validation.process_invoice(frappe._dict(name=invoice.name, creation=invoice.creation))
            fallback.assert_not_called()
        for _ in range(2):
            fire_items(invoice.name, line=invoice.items[0].name)
            ury_kot_validation.process_invoice(frappe._dict(name=invoice.name, creation=invoice.creation))
        ticket, = self.tickets(invoice.name)
        row, = frappe.get_doc('URY KOT', ticket).kot_items
        self.assertEqual((row.comments, row.quantity), ('seat-1', 1))

    def test_mixed_desk_snapshot_uses_persisted_row_identity_and_hold(self):
        invoice = self.order((self.c.item, 'seat-1', True), (self.c.item, 'seat-2', True))
        # Simulate the persisted Desk save; the callback supplies only ordinary Desk row names.
        invoice.items[1].db_set('ury_held', 0)
        kot_execute(invoice.name, invoice.customer, None, [dict(name=r.name, item_code=r.item_code,
                    item_name=r.item_name, qty=r.qty, comments='') for r in invoice.items])
        ticket, = self.tickets(invoice.name)
        self.assertEqual([(r.reservation_line_key, r.comments) for r in frappe.get_doc('URY KOT', ticket).kot_items],
                         [(f'ref:{self.c.item.name}:seat-2', 'seat-2')])

    def test_split_moves_whole_released_ticket_to_its_reservation(self):
        dish = make_made_to_order_item(self.c)
        invoice = self.order((dish, 'move', False), (dish, 'stay', True))
        ticket, = self.tickets(invoice.name)
        result = split_bill(invoice.name, [dict(name=invoice.items[0].name, qty=1)])
        ticket = frappe.get_doc('URY KOT', ticket)
        self.assertEqual(ticket.invoice, result['new_invoice'])
        self.assert_reserved(ticket, dish)

    def assert_split_refused_without_writes(self, invoice, move_qty):
        def snapshot():
            return (
                invoice.reload().as_dict(),
                frappe.get_all('POS Invoice', filters={'custom_split_from': invoice.name}, pluck='name'),
                self.tickets(invoice.name),
                frappe.get_all('URY Stock Reservation', filters={'branch': self.c.branch.name},
                               fields=['*'], order_by='name'),
            )

        before = snapshot()
        self.assertTrue(before[2] and before[3])
        with self.assertRaisesRegex(frappe.ValidationError, 'URY-SPLIT-RESERVED-KOT'):
            split_bill(invoice.name, [dict(name=invoice.items[0].name, qty=move_qty)])
        self.assertEqual(snapshot(), before)

    def test_refuses_one_released_line_of_a_reserved_mixed_ticket(self):
        dish = make_made_to_order_item(self.c)
        invoice = self.order((dish, 'move', False), (dish, 'stay', False))
        ticket, = self.tickets(invoice.name)
        self.assertEqual(len(frappe.get_doc('URY KOT', ticket).kot_items), 2)
        self.assert_split_refused_without_writes(invoice, 1)

    def test_refuses_partial_quantity_of_a_reserved_released_line(self):
        dish = make_made_to_order_item(self.c)
        invoice = frappe.get_doc('POS Invoice', send_order(self.c, items=[dict(
            item=dish.name, item_name=dish.item_name, qty=2, rate=100, reservation_line_key='move',
        )]).name)
        self.assert_split_refused_without_writes(invoice, 1)

    def test_held_move_keeps_the_released_ticket_on_source(self):
        dish = make_made_to_order_item(self.c)
        invoice = self.order((dish, 'stay', False), (dish, 'held', True))
        ticket, = self.tickets(invoice.name)
        result = split_bill(invoice.name, [dict(name=invoice.items[1].name, qty=1)])
        self.assertEqual(self.tickets(invoice.name), [ticket])
        self.assert_reserved(frappe.get_doc('URY KOT', ticket), dish)
        sibling = frappe.get_doc('POS Invoice', result['new_invoice'])
        self.assertTrue(sibling.items[0].ury_held)
        self.assertFalse(self.tickets(sibling.name))

    def test_guest_repeated_dishes_keep_separate_native_reservations_and_price(self):
        dish = make_made_to_order_item(self.c)
        frappe.set_user('Administrator')
        frappe.defaults.set_user_default('company', self.c.restaurant.company)
        profile = frappe.get_doc(dict(doctype='URY Self Ordering Profile', name=self.c.profile.name, profile_name=self.c.profile.name,
            branch=self.c.branch.name, restaurant=self.c.restaurant.name, pos_profile=self.c.profile.name,
            default_customer=self.c.customer.name, enabled=1, allow_add_to_running_table=1)).insert()
        with patch.object(frappe.db, 'commit'):
            token, session = self_ordering._open_session(profile, 'QR Pickup', None)
        frappe.set_user('Guest')
        for _ in range(2):
            self_ordering.add_customer_items(token, [dict(item=dish.name, qty=1, comment='warm', rate=1)])
        frappe.set_user('Administrator')
        invoice = frappe.get_doc('POS Invoice', frappe.db.get_value('URY Ordering Session', session.name, 'invoice'))
        self.assertEqual([r.rate for r in invoice.items], [100, 100])
        tickets = [frappe.get_doc('URY KOT', name) for name in self.tickets(invoice.name)]
        self.assertEqual(len(tickets), 2)
        self.assertEqual(len({t.kot_items[0].reservation_line_key for t in tickets}), 2)
        for ticket in tickets:
            self.assert_reserved(ticket, dish)

    def test_zero_reduction_releases_held_reservation_without_ticket(self):
        dish = make_made_to_order_item(self.c)
        invoice = self.order((dish, 'drop', True), (dish, 'keep', False))
        before = self.tickets(invoice.name)
        result = reduce_order_item_qty(invoice.name, invoice.items[0].name, 0)
        self.assertEqual(result['cancel_kot_names'], [])
        self.assertEqual(self.tickets(invoice.name), before)
        self.assertEqual([row.reservation_line_key for row in invoice.reload().items], ['keep'])
        remaining = frappe.get_all('URY Stock Reservation', filters=dict(order_ref=invoice.name,
            top_level_item=dish.name, status='Reserved'), fields=['audit_log'])
        self.assertEqual({_row_reservation_line_key(row) for row in remaining}, {f'ref:{dish.name}:keep'})
