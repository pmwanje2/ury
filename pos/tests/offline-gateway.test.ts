import { readFileSync } from 'node:fs';
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
const KNOWN_ID = '11111111-1111-4111-8111-111111111111';

const json = (message: unknown, status = 200) => new Response(JSON.stringify({ message }), {
  status, headers: { 'Content-Type': 'application/json' },
});

beforeEach(async () => {
  await initI18n('en');
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('navigator', { onLine: true, storage: { persist: vi.fn(async () => true) }, locks: testLocks() });
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write: vi.fn(), close: vi.fn() }, focus: vi.fn(), print: vi.fn(), opener: null }) as any);
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function captureModule() {
  return import('../src/offline/cashier');
}

async function queuedIds() {
  const { OfflineOutbox } = await import('../src/offline/outbox');
  return (await new OfflineOutbox({ profile, user }).list()).map((row) => row.client_order_id);
}

test('the invoice list exposes the client order id so a lost settlement can be deduplicated', () => {
  const source = readFileSync(resolve('ury/ury_pos/api.py'), 'utf8');
  const list = source.slice(source.indexOf('def getPosInvoice'), source.indexOf('def searchPosInvoice'));
  const search = source.slice(source.indexOf('def searchPosInvoice'), source.indexOf('def get_select_field_options'));
  expect(list.match(/ury_client_order_id/g)?.length).toBeGreaterThanOrEqual(5);
  expect(search).toContain('ury_client_order_id');
});

test('an opted-in live order sends a client order id and does not queue when the server answers', async () => {
  const { submitOrderOfflineAware } = await captureModule();
  let sent: { clientOrderId?: string; roundId?: string } | undefined;
  const result = await submitOrderOfflineAware({
    profile, user, order: order as any, total: 100,
    onlineSubmit: async (ids?: { clientOrderId: string; roundId: string }) => {
      sent = ids;
      return { message: { name: 'INV-1' } };
    },
  });
  expect(result).toEqual({ offline: false, response: { message: { name: 'INV-1' } } });
  expect(sent?.clientOrderId).toMatch(/^[0-9a-f-]{36}$/);
  expect(sent?.roundId).toMatch(/^[0-9a-f-]{36}$/);
  expect(await queuedIds()).toEqual([]);
});

test.each([502, 504])('HTTP %i on a new order is queued with the same client order id and round id', async (status) => {
  const { submitOrderOfflineAware } = await captureModule();
  let sent: { clientOrderId: string; roundId: string } | undefined;
  const result = await submitOrderOfflineAware({
    profile, user, order: order as any, total: 100,
    onlineSubmit: async (ids?: { clientOrderId: string; roundId: string }) => {
      sent = ids;
      throw { httpStatus: status, message: 'gateway' };
    },
  });
  expect(result.offline).toBe(true);
  if (!result.offline) return;
  expect(sent?.clientOrderId).toMatch(/^[0-9a-f-]{36}$/);
  expect(result.entry.client_order_id).toBe(sent?.clientOrderId);
  expect(result.entry.rounds[0].round_id).toBe(sent?.roundId);
  expect(await queuedIds()).toEqual([sent?.clientOrderId]);

  const sends: Array<{ method: string; data: any }> = [];
  const { OfflineOutbox } = await import('../src/offline/outbox');
  const queue = new OfflineOutbox({
    profile, user, print: async () => true,
    request: async (url: string, init?: RequestInit) => {
      const method = String(url).split('/api/method/')[1]?.split('?')[0] || '';
      const data = init?.body ? JSON.parse(String(init.body)) : null;
      sends.push({ method, data });
      if (method === 'frappe.auth.get_logged_user') return json(user.name);
      if (method.endsWith('sync_order')) return json({ name: 'INV-REPLAY', docstatus: 0 });
      return json(true);
    },
  });
  await queue.replay();
  const sync = sends.find((row) => row.method.endsWith('sync_order'));
  expect(sync?.data.client_order_id).toBe(sent?.clientOrderId);
  expect(sync?.data.round_id).toBe(sent?.roundId);
  expect(await queue.list()).toEqual([]);
});

test('an axios-shaped 503 is queued the same way as httpStatus', async () => {
  const { submitOrderOfflineAware } = await captureModule();
  let sent: { clientOrderId?: string } | undefined;
  const result = await submitOrderOfflineAware({
    profile, user, order: order as any, total: 100,
    onlineSubmit: async (ids?: { clientOrderId: string }) => {
      sent = ids;
      throw { response: { status: 503 }, message: 'maintenance' };
    },
  });
  expect(result.offline).toBe(true);
  if (!result.offline) return;
  expect(result.entry.client_order_id).toBe(sent?.clientOrderId);
});

test.each([400, 401, 403, 500])('HTTP %i on a new order is not queued and stays an error', async (status) => {
  const { submitOrderOfflineAware } = await captureModule();
  const onlineSubmit = vi.fn(async () => { throw { httpStatus: status, exc_type: 'ServerError' }; });
  await expect(submitOrderOfflineAware({ profile, user, order: order as any, total: 100, onlineSubmit })).rejects.toMatchObject({ httpStatus: status });
  expect(onlineSubmit).toHaveBeenCalledOnce();
  expect(await queuedIds()).toEqual([]);
});

test('a gateway error on an opted-out till or an already-open invoice is not queued', async () => {
  const { submitOrderOfflineAware } = await captureModule();
  const gateway = async () => { throw { httpStatus: 502, message: 'gateway' }; };
  await expect(submitOrderOfflineAware({
    profile: { ...profile, ury_offline_capture: 0 }, user, order: order as any, total: 100, onlineSubmit: gateway,
  })).rejects.toMatchObject({ httpStatus: 502 });
  await expect(submitOrderOfflineAware({
    profile, user, order: { ...order, invoice: 'ONLINE-1' } as any, total: 100, onlineSubmit: gateway,
  })).rejects.toMatchObject({ httpStatus: 502 });
  expect(await queuedIds()).toEqual([]);
});

test('a 504 on cash settlement is queued with the client order id sent to the server', async () => {
  const { submitSettlementOfflineAware } = await captureModule();
  let sent: { clientOrderId: string; roundId: string } | undefined;
  const result = await submitSettlementOfflineAware({
    profile, user, invoice: 'INV-9', customer: 'Walk In', table: 'T-1', cashier: user.name, owner: user.name,
    clientOrderId: KNOWN_ID, amount: 100, modeOfPayment: 'Cash',
    onlineSettle: async (ids) => {
      sent = ids;
      throw { httpStatus: 504, message: 'timeout' };
    },
  });
  expect(result.offline).toBe(true);
  if (!result.offline) return;
  expect(sent?.clientOrderId).toBe(KNOWN_ID);
  expect(result.entry.client_order_id).toBe(KNOWN_ID);
  expect(result.entry.rounds.at(-1)?.round_id).toBe(sent?.roundId);
  expect(result.entry.server_invoice).toBe('INV-9');
  expect(result.entry.phase).toBe('settle');
  expect(result.entry.settlement).toMatchObject({ kind: 'cash', mode_of_payment: 'Cash', amount: 100 });

  const sends: Array<{ method: string; data: any }> = [];
  const { OfflineOutbox } = await import('../src/offline/outbox');
  const queue = new OfflineOutbox({
    profile, user, print: async () => true,
    request: async (url: string, init?: RequestInit) => {
      const method = String(url).split('/api/method/')[1]?.split('?')[0] || '';
      const data = init?.body ? JSON.parse(String(init.body)) : null;
      sends.push({ method, data });
      if (method === 'frappe.auth.get_logged_user') return json(user.name);
      if (method.endsWith('make_invoice')) return json({ name: 'INV-9', docstatus: 1 });
      return json(true);
    },
  });
  await queue.replay();
  expect(sends.map((row) => row.method.split('.').pop())).toEqual(['get_logged_user', 'make_invoice']);
  const settle = sends[1].data;
  expect(settle).toMatchObject({
    invoice: 'INV-9', client_order_id: KNOWN_ID, round_id: sent?.roundId,
    customer: 'Walk In', table: 'T-1', payments: [{ mode_of_payment: 'Cash', amount: 100 }],
  });
  expect(await queue.list()).toEqual([]);
});

test.each([
  ['500', { httpStatus: 500 }, 'Cash', 0],
  ['401', { httpStatus: 401 }, 'Cash', 0],
  ['403', { httpStatus: 403 }, 'Cash', 0],
  ['card', { httpStatus: 502 }, 'Card', 0],
  ['discount', { httpStatus: 502 }, 'Cash', 10],
])('settlement %s is not treated as offline', async (_name, error, mode, discount) => {
  const { submitSettlementOfflineAware } = await captureModule();
  await expect(submitSettlementOfflineAware({
    profile, user, invoice: 'INV-9', customer: 'Walk In', cashier: user.name, owner: user.name,
    amount: 100, modeOfPayment: mode, discount,
    onlineSettle: async () => { throw error; },
  })).rejects.toEqual(error);
  expect(await queuedIds()).toEqual([]);
});

test('an opted-out till does not queue a gateway settlement', async () => {
  const { submitSettlementOfflineAware } = await captureModule();
  await expect(submitSettlementOfflineAware({
    profile: { ...profile, ury_offline_capture: 0 }, user, invoice: 'INV-9', customer: 'Walk In',
    cashier: user.name, owner: user.name, amount: 100, modeOfPayment: 'Cash',
    onlineSettle: async () => { throw { httpStatus: 520, message: 'cloudflare' }; },
  })).rejects.toMatchObject({ httpStatus: 520 });
  expect(await queuedIds()).toEqual([]);
});
