import { afterEach, expect, it, vi } from 'vitest';

// Ported from the fork (split-bill-guest-label-api.test.ts and the
// mapSplitGroupInvoiceToPOSInvoice case of guest-label-carry-through, M03).

const post = vi.hoisted(() => vi.fn());
vi.mock('@ury/core', () => ({ call: { post, get: vi.fn() }, db: {} }));

import { splitBill } from './order-api';
import { mapSplitGroupInvoiceToPOSInvoice, type SplitGroupInvoice } from './invoice-api';

afterEach(() => post.mockReset());

it('splitBill forwards a trimmed guest label to split_bill', async () => {
  post.mockResolvedValue({ message: { source_invoice: 'A', new_invoice: 'B' } });
  await splitBill('A', [{ name: 'line-1', qty: 1 }], undefined, ' David ');
  expect(post).toHaveBeenCalledExactlyOnceWith('ury.ury.doctype.ury_order.ury_order.split_bill', {
    source_invoice: 'A', items_to_move: [{ name: 'line-1', qty: 1 }], customer: undefined, guest_label: 'David',
  });
});

it('splitBill without a label sends none', async () => {
  post.mockResolvedValue({ message: { source_invoice: 'A', new_invoice: 'B' } });
  await splitBill('A', [{ name: 'line-1', qty: 1 }]);
  expect(post.mock.calls[0][1]).not.toHaveProperty('guest_label');
});

it('mapSplitGroupInvoiceToPOSInvoice keeps the guest label and customer name', () => {
  const mapped = mapSplitGroupInvoiceToPOSInvoice({
    name: 'INV-B', customer: 'Walk In', customer_name: 'Walk In', custom_guest_label: 'David',
    split_index: 2, split_total: 2, is_original: false, docstatus: 0,
  } as unknown as SplitGroupInvoice);
  expect(mapped.custom_guest_label).toBe('David');
  expect(mapped.customer_name).toBe('Walk In');
});
