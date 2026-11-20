// Ported from fork pos/tests/till-close-api.test.mjs (799dc28e, React part):
// node:test + vm harness converted to Vitest.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { getMainCashierPosInvoices, getSubCashierPosInvoices } from './pos-closing-api';

const params = {
  start: '2026-10-01 08:00:00', end: '2026-10-01 18:00:00',
  pos_profile: 'Till', user: 'cashier2',
};
const invoice = {
  name: 'OTHER-CASHIER', cashier: 'cashier1', owner: 'waiter',
  grand_total: 15000, net_total: 15000, total_qty: 1, taxes: [],
  payments: [{ mode_of_payment: 'Cash', amount: 15000 }],
  modified: '2026-10-01 12:00:00',
};

afterEach(() => vi.restoreAllMocks());

describe('till close invoice reads', () => {
  it('main close requests the whole till and returns full invoice documents', async () => {
    const get = vi.spyOn(call, 'get').mockResolvedValue({ message: [invoice] });
    const result = await getMainCashierPosInvoices(params.start, params.end, 'Till', 'cashier2');
    expect(get).toHaveBeenCalledExactlyOnceWith('ury.ury_pos.api.get_till_invoices', params);
    expect(result).toEqual([invoice]);
  });

  it('sub-cashier close keeps its separate per-cashier endpoint', async () => {
    const get = vi.spyOn(call, 'get').mockResolvedValue({ message: [] });
    await getSubCashierPosInvoices(params.start, params.end, 'Till', 'cashier2');
    expect(get).toHaveBeenCalledExactlyOnceWith(
      'ury.ury.doctype.sub_pos_closing.sub_pos_closing.get_pos_invoices', params,
    );
  });
});
