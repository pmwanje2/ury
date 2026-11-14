import { expect, it } from 'vitest';
import { getOrdersTabForInvoice } from '../src/lib/invoice-api';

// The Orders tab a sibling bill opens in must match the server queues
// (ury/ury_pos/api.py getPosInvoice): Unbilled is an unprinted table bill;
// a printed bill or an untabled order is Draft.
it('an unprinted table bill opens in Unbilled', () => {
  expect(getOrdersTabForInvoice({ invoice_printed: 0, restaurant_table: 'T1', status: 'Draft' })).toBe('Unbilled');
});

it('a printed table bill opens in Draft', () => {
  expect(getOrdersTabForInvoice({ invoice_printed: 1, restaurant_table: 'T1', status: 'Draft' })).toBe('Draft');
});

it('an unprinted order without a table opens in Draft', () => {
  expect(getOrdersTabForInvoice({ invoice_printed: 0, restaurant_table: null, status: 'Draft' })).toBe('Draft');
});
