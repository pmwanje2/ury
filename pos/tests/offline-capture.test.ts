import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { testLocks } from './offline-locks';
import { initI18n } from '../src/i18n';

const profile = { name: 'Till A', ury_offline_capture: 1, role_allowed_for_billing: [{ role: 'Cashier' }] };
const user = { name: 'cashier@example.com', roles: ['Cashier'] };
const order = { pos_profile: 'Till A', customer: 'Walk In', cashier: user.name, owner: user.name, waiter: user.name,
  items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 100 }], no_of_pax: 1, invoice: null, last_invoice: null,
  mode_of_payment: 'Cash', order_type: 'Take Away' };

beforeEach(async () => {
  await initI18n('en');
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('navigator', { onLine: false, storage: { persist: vi.fn(async () => true) }, locks: testLocks() });
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write: vi.fn(), close: vi.fn() }, focus: vi.fn(), print: vi.fn() }) as any);
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function captureModule() {
  const present = existsSync(resolve('pos/src/offline/cashier.ts'));
  expect(present, 'the cashier offline submission seam is missing').toBe(true);
  if (!present) throw new Error('expected RED failure above');
  const path = '../src/offline/cashier.ts';
  return import(/* @vite-ignore */ path);
}

test('cashier submission captures a new order offline, prints KOT and leaves the cart caller a provisional bill', async () => {
  const { submitOrderOfflineAware } = await captureModule();
  const onlineSubmit = vi.fn(async () => { throw new TypeError('Failed to fetch'); });
  const result = await submitOrderOfflineAware({ profile, user, order: order as any, total: 100, onlineSubmit });
  expect(result).toMatchObject({ offline: true, entry: { rounds: [{ offline_printed: 1, expected_total: 100 }] } });
  expect(onlineSubmit).not.toHaveBeenCalled();
  expect(window.open).toHaveBeenCalledOnce();
  expect((window.open as any).mock.results[0].value.document.write).toHaveBeenCalledWith(expect.stringContaining('Kitchen Order Ticket'));
});

test('an online-opened order and a non-network refusal are never captured as an offline round', async () => {
  const { submitOrderOfflineAware } = await captureModule();
  const onlineSubmit = vi.fn(async () => { throw new Error('URY_OFFLINE_TOTAL_MISMATCH'); });
  await expect(submitOrderOfflineAware({ profile, user, order: { ...order, invoice: 'ONLINE-1' } as any, total: 100, onlineSubmit })).rejects.toThrow(/online|offline/i);
  navigator.onLine = true;
  await expect(submitOrderOfflineAware({ profile: { ...profile }, user, order: order as any, total: 100, onlineSubmit })).rejects.toThrow('URY_OFFLINE_TOTAL_MISMATCH');
  expect(onlineSubmit).toHaveBeenCalledTimes(1);
});

test('a queued offline cash settlement prints the provisional bill and manual mobile money requires a reference', async () => {
  const { submitOrderOfflineAware, settleOfflineOrder } = await captureModule();
  const result = await submitOrderOfflineAware({ profile, user, order: order as any, total: 100, onlineSubmit: vi.fn() });
  await expect(settleOfflineOrder({ profile, user, clientOrderId: result.entry.client_order_id,
    kind: 'mobile-money', modeOfPayment: 'Mobile Money', amount: 100, reference: '' })).rejects.toThrow(/reference/i);
  await settleOfflineOrder({ profile, user, clientOrderId: result.entry.client_order_id,
    kind: 'cash', modeOfPayment: 'Cash', amount: 100 });
  expect(window.open).toHaveBeenCalledTimes(2);
  expect((window.open as any).mock.results[1].value.document.write).toHaveBeenCalledWith(expect.stringContaining('Provisional bill — not a fiscal receipt'));
});

test('station browser print escapes untrusted item names and never labels a provisional bill fiscal', async () => {
  const { submitOrderOfflineAware, settleOfflineOrder } = await captureModule();
  const result = await submitOrderOfflineAware({ profile, user, order: { ...order,
    items: [{ ...order.items[0], item_name: '<img src=x onerror=alert(1)>' }] }, total: 100, onlineSubmit: vi.fn() });
  await settleOfflineOrder({ profile, user, clientOrderId: result.entry.client_order_id, kind: 'cash', modeOfPayment: 'Cash', amount: 100 });
  const html = (window.open as any).mock.results[1].value.document.write.mock.calls[0][0];
  expect(html).toContain('&lt;img'); expect(html).not.toContain('<img');
  expect(html).toContain('Provisional bill — not a fiscal receipt');
});

test('a blocked station print can be acknowledged by reprinting before replay', async () => {
  const { OfflineOutbox } = await import('../src/offline/outbox');
  const print = vi.fn(() => false);
  const q = new OfflineOutbox({ profile, user, print });
  const entry = await q.capture(order as any, 100);
  expect(entry.rounds[0].offline_printed).toBe(0);
  print.mockReturnValue(true);
  expect(q.reprint, 'blocked print has no acknowledgement/reprint path').toBeTypeOf('function');
  await q.reprint(entry.client_order_id, 'kot');
  expect((await q.list())[0].rounds[0].offline_printed).toBe(1);
  expect(print).toHaveBeenCalledTimes(2);
});

test.each(['void', 'reduction', 'discount', 'transfer', 'merge', 'split', 'card', 'pay-by-phone', 'efris-fdn', 'till-close'])
  ('offline %s is refused with an operator-facing reason', async (action) => {
    const { assertOfflineActionAllowed } = await import('../src/offline/cashier');
    expect(assertOfflineActionAllowed, 'refused offline actions have no guard').toBeTypeOf('function');
    await expect(assertOfflineActionAllowed(action)).rejects.toThrow(/offline|refused|not available/i);
  });
