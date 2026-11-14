import { afterEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { splitBill } from '../src/lib/order-api';

vi.mock('../src/offline/cashier', () => ({ assertOfflineActionAllowed: vi.fn(async () => {}) }));

afterEach(() => vi.restoreAllMocks());

it('splitBill forwards a guest label to split_bill', async () => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { source_invoice: 'A', new_invoice: 'B' } });
  await splitBill('A', [{ name: 'line-1', qty: 1 }], undefined, 'David');
  expect(post).toHaveBeenCalledExactlyOnceWith('ury.ury.doctype.ury_order.ury_order.split_bill', {
    source_invoice: 'A', items_to_move: [{ name: 'line-1', qty: 1 }], customer: undefined, guest_label: 'David',
  });
});
