import { afterEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import { syncOrder, splitBill, tableTransfer, captainTransfer } from '../src/lib/order-api';
import { mergeBills } from '../src/lib/invoice-api';
import { printOrder } from '../src/lib/print';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
test.each([
  ['online-order rounds/reductions', () => syncOrder({ invoice: 'ONLINE-1' } as any)],
  ['split', () => splitBill('ONLINE-1', [{ name: 'ROW', qty: 1 }])],
  ['table transfer', () => tableTransfer('A', 'B', 'ONLINE-1')],
  ['captain transfer', () => captainTransfer('A', 'B', 'ONLINE-1')],
  ['merge', () => mergeBills('A', 'B')],
  ['fiscal print', () => printOrder({ orderId: 'ONLINE-1', posProfile: { print_type: 'socket' } as any })],
])('%s is explicitly refused offline before a live mutation', async (_action, run) => {
  vi.stubGlobal('navigator', { onLine: false });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: {} });
  vi.spyOn(window, 'open').mockReturnValue(null);
  await expect(run()).rejects.toThrow(/offline|refused|not available/i);
  expect(post).not.toHaveBeenCalled();
});
