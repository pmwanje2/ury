import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { testLocks } from './offline-locks';

const profile = { name: 'Till A', ury_offline_capture: 1, role_allowed_for_billing: [{ role: 'Cashier' }] };
const user = { name: 'cashier@example.com', roles: ['Cashier'] };
const data = { pos_profile: 'Till A', customer: 'Walk In', cashier: user.name, owner: user.name,
  waiter: user.name, items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 100 }],
  no_of_pax: 1, invoice: null, last_invoice: null, mode_of_payment: 'Cash', order_type: 'Take Away' };
let sends: Array<{ method: string; data: any }>;
let request: ReturnType<typeof vi.fn>;
let print: ReturnType<typeof vi.fn>;
let persist: ReturnType<typeof vi.fn>;

const json = (message: unknown, status = 200) => new Response(JSON.stringify({ message }), {
  status, headers: { 'Content-Type': 'application/json' },
});

// Fail explicitly on the missing capture behavior, rather than an import/setup error.
async function outbox(options: Record<string, unknown> = {}) {
  expect(existsSync(resolve('pos/src/offline/outbox.ts')), 'durable offline capture/replay is not implemented').toBe(true);
  const path = '../src/offline/outbox.ts';
  const { OfflineOutbox } = await import(/* @vite-ignore */ path);
  return new OfflineOutbox({ profile, user, request, print, ...options });
}

beforeEach(() => {
  vi.stubGlobal('indexedDB', new IDBFactory());
  sends = [];
  persist = vi.fn(async () => true);
  vi.stubGlobal('navigator', { onLine: false, storage: { persist }, locks: testLocks() });
  print = vi.fn(async () => true);
  request = vi.fn(async (url: string, init?: RequestInit) => {
    const method = url.split('/api/method/')[1]?.split('?')[0];
    if (method === 'frappe.auth.get_logged_user') return json(user.name);
    const payload = JSON.parse(String(init?.body));
    sends.push({ method, data: payload });
    if (method.endsWith('sync_order')) return json({ name: `INV-${payload.customer}`, docstatus: 0 });
    if (method.endsWith('make_invoice')) return json({ name: payload.invoice, docstatus: 1 });
    return json(true);
  });
});

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test('captures durably in IndexedDB and requests persistent storage before acknowledging', async () => {
  const q = await outbox();
  const entry = await q.capture(data, 100);
  expect(entry.client_order_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(entry.rounds[0]).toMatchObject({ expected_total: 100, offline_printed: 1 });
  expect(entry.rounds[0].captured_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  expect((await (await outbox()).list()).map((row: any) => row.client_order_id)).toEqual([entry.client_order_id]);
  expect(persist).toHaveBeenCalledOnce();
  expect(sends).toEqual([]);
});

test('FIFO replays three orders serially and settles only the cash-paid order after print-status', async () => {
  const q = await outbox();
  const a = await q.capture({ ...data, customer: 'A' }, 100);
  await q.capture({ ...data, customer: 'B' }, 100);
  await q.capture({ ...data, customer: 'C' }, 100);
  await q.settle(a.client_order_id, { kind: 'cash', mode_of_payment: 'Cash', amount: 100 });
  await q.replay();
  expect(sends.map((s) => [s.method.split('.').pop(), s.data.customer || s.data.invoice])).toEqual([
    ['sync_order', 'A'], ['qz_print_update', 'INV-A'], ['make_invoice', 'A'],
    ['sync_order', 'B'], ['sync_order', 'C'],
  ]);
  expect(sends[0].data).toMatchObject({ append: 1, client_order_id: a.client_order_id,
    round_id: a.rounds[0].round_id, captured_at: a.rounds[0].captured_at, offline_printed: 1, expected_total: 100 });
  expect(sends[2].data).toMatchObject({ client_order_id: a.client_order_id, round_id: a.rounds[0].round_id });
  expect(await q.list()).toEqual([]);
});

test('a network error keeps the head item and does not send the next order', async () => {
  const q = await outbox();
  const a = await q.capture(data, 100);
  await q.capture({ ...data, customer: 'B' }, 100);
  request.mockImplementationOnce(async () => json(user.name)).mockImplementationOnce(async () => { throw new TypeError('Failed to fetch'); });
  await q.replay();
  expect((await q.list()).map((r: any) => r.state)).toEqual(['pending', 'pending']);
  expect((await q.list())[0].client_order_id).toBe(a.client_order_id);
  expect(sends).toEqual([]);
});

test.each(['URY_OFFLINE_OUTSIDE_SHIFT', 'URY_OFFLINE_TOTAL_MISMATCH', 'PermissionError', 'InsufficientStockError', 'SessionExpired'])('%s parks the order without dropping it and permits the next order', async (refusal) => {
  const q = await outbox();
  await q.capture(data, 100);
  await q.capture({ ...data, customer: 'B' }, 100);
  request.mockImplementationOnce(async () => json(user.name)).mockImplementationOnce(async () =>
    new Response(JSON.stringify({ exc_type: refusal, _server_messages: JSON.stringify([JSON.stringify({ message: refusal })]) }), {
      status: 417, headers: { 'Content-Type': 'application/json' },
    }));
  await q.replay();
  const rows = await q.list();
  expect(rows).toHaveLength(1);
  expect(rows[0]).toMatchObject({ state: 'review' });
  expect(rows[0].reason).toContain(refusal);
  expect(sends.map((s) => s.data.customer)).toEqual(['B']);
});

test.each([
  new Response(JSON.stringify({ exc_type: 'SessionExpired' }), { status: 401, headers: { 'Content-Type': 'application/json' } }),
  new Response(JSON.stringify({ exc_type: 'CSRFTokenError' }), { status: 400, headers: { 'Content-Type': 'application/json' } }),
  new Response(JSON.stringify({ exc_type: 'PermissionError', exception: 'Login to access this method' }), { status: 403, headers: { 'Content-Type': 'application/json' } }),
])('transient authentication responses retain the queued order for a later retry', async (response) => {
  const q = await outbox();
  await q.capture(data, 100);
  request.mockImplementationOnce(async () => json(user.name)).mockImplementationOnce(async () => response);
  await q.replay();
  expect((await q.list())[0].state).toBe('pending');
});

test('ids and captured_at survive a lost order acknowledgement and a reload', async () => {
  const q = await outbox();
  const row = await q.capture(data, 100);
  request.mockImplementationOnce(async () => json(user.name)).mockImplementationOnce(async (_url, init) => {
    sends.push({ method: 'sync_order', data: JSON.parse(init.body) });
    throw new TypeError('Network Error after commit');
  });
  await q.replay();
  await (await outbox()).replay();
  expect(sends).toHaveLength(2);
  for (const send of sends) expect(send.data).toMatchObject({ client_order_id: row.client_order_id,
    round_id: row.rounds[0].round_id, captured_at: row.rounds[0].captured_at });
});

test('a lost settlement acknowledgement resumes settlement without resending earlier rounds', async () => {
  const q = await outbox();
  const row = await q.capture(data, 100);
  await q.settle(row.client_order_id, { kind: 'cash', mode_of_payment: 'Cash', amount: 100 });
  const normal = request.getMockImplementation()!;
  request.mockImplementation(async (url, init) => {
    if (url.endsWith('make_invoice')) {
      sends.push({ method: 'make_invoice', data: JSON.parse(init.body) });
      throw new TypeError('Lost acknowledgement');
    }
    return normal(url, init);
  });
  await q.replay();
  request.mockImplementation(normal);
  await (await outbox()).replay();
  expect(sends.map((s) => s.method.split('.').pop())).toEqual(['sync_order', 'qz_print_update', 'make_invoice', 'make_invoice']);
  expect(sends[2].data).toEqual(sends[3].data);
  expect(await q.list()).toEqual([]);
});

test.each([
  new Response('<html>Cloudflare Access Login</html>', { headers: { 'Content-Type': 'text/html' } }),
  new Response(null, { status: 302, headers: { Location: '/login' } }),
  json('Guest'),
])('a login/Access redirect or HTML/Guest ping is not success', async (response) => {
  const q = await outbox();
  await q.capture(data, 100);
  request.mockImplementationOnce(async () => response);
  await q.replay();
  expect((await q.list())[0].state).toBe('pending');
  expect(sends).toEqual([]);
});

test('a login redirect on an action retains that order instead of parking or acknowledging', async () => {
  const q = await outbox();
  await q.capture(data, 100);
  request.mockImplementationOnce(async () => json(user.name)).mockImplementationOnce(async () =>
    new Response('<html>Login</html>', { headers: { 'Content-Type': 'text/html' } }));
  await q.replay();
  expect((await q.list())[0].state).toBe('pending');
});

test('positive extra rounds only attach to an un-settled order from this same outage', async () => {
  const q = await outbox();
  const first = await q.capture(data, 100);
  const next = await q.capture(data, 200, first.client_order_id);
  expect(next.rounds).toHaveLength(2);
  expect(next.rounds[1].round_id).not.toBe(first.rounds[0].round_id);
  await expect(q.capture({ ...data, items: [{ ...data.items[0], qty: -1 }] }, 100, first.client_order_id)).rejects.toThrow(/positive|reduction/i);
  await expect(q.capture({ ...data, invoice: 'ONLINE-1' }, 100)).rejects.toThrow(/online/i);
  await q.settle(first.client_order_id, { kind: 'cash', mode_of_payment: 'Cash', amount: 200 });
  await expect(q.capture(data, 300, first.client_order_id)).rejects.toThrow(/settled|settlement/i);
});

test('offline opt-in and this profile billing role are both required (no admin or empty-role shortcut)', async () => {
  for (const options of [{ profile: { ...profile, ury_offline_capture: 0 } }, { user: { ...user, roles: ['Waiter'] } },
    { profile: { ...profile, role_allowed_for_billing: [] } }, { user: { name: 'Guest', roles: ['Cashier'] } }]) {
    await expect((await outbox(options)).capture(data, 100)).rejects.toThrow(/offline|billing|cashier/i);
  }
});

test('revoking the cached billing role prevents replay before any server mutation', async () => {
  const mutable = { ...profile, role_allowed_for_billing: [{ role: 'Cashier' }] };
  const q = await outbox({ profile: mutable }); await q.capture(data, 100);
  mutable.role_allowed_for_billing = [];
  await q.replay();
  expect(sends).toEqual([]);
  expect((await q.list())[0].state).toBe('pending');
});

test('a hung request releases Replaying after a bounded timeout without dropping the order', async () => {
  const q = await outbox(); await q.capture(data, 100);
  request.mockImplementationOnce((_url, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new Error('Timed out')));
  }));
  vi.useFakeTimers();
  const replay = q.replay();
  await vi.advanceTimersByTimeAsync(11000);
  expect(q.replaying).toBe(false);
  vi.useRealTimers(); await replay;
  expect((await q.list())[0].state).toBe('pending');
});

test('new extra rounds cannot rewrite the offline order customer or table', async () => {
  const q = await outbox(); const row = await q.capture(data, 100);
  await expect(q.capture({ ...data, customer: 'Other Guest' }, 200, row.client_order_id)).rejects.toThrow(/customer|context|change|transfer/i);
  expect((await q.list())[0].rounds).toHaveLength(1);
});

test('manual MoMo requires a typed reference and retains it in the native order comments', async () => {
  const q = await outbox();
  const row = await q.capture(data, 100);
  await expect(q.settle(row.client_order_id, { kind: 'mobile-money', mode_of_payment: 'Mobile Money', amount: 100, reference: ' ' })).rejects.toThrow(/reference/i);
  await q.settle(row.client_order_id, { kind: 'mobile-money', mode_of_payment: 'Mobile Money', amount: 100, reference: 'MM-123' });
  await q.replay();
  expect(sends[0].data.comments).toContain('MM-123');
  expect(sends[2].data.payments).toEqual([{ mode_of_payment: 'Mobile Money', amount: 100 }]);
});

test.each(['card', 'pay-by-phone'])('offline %s tender is refused and never queued as settlement', async (kind) => {
  const q = await outbox();
  const row = await q.capture(data, 100);
  await expect(q.settle(row.client_order_id, { kind, mode_of_payment: 'Card', amount: 100 })).rejects.toThrow(/cash|mobile|offline/i);
  expect((await q.list())[0].settlement).toBeUndefined();
});

test('logout/purge stays blocked for pending AND parked records; another cashier cannot replay them', async () => {
  const q = await outbox();
  await q.capture(data, 100);
  await expect(q.assertEmpty()).rejects.toThrow(/pending|review|outbox/i);
  const other = await outbox({ user: { name: 'other@example.com', roles: ['Cashier'] } });
  expect(await other.list()).toEqual([]);
  await expect(other.assertEmpty()).rejects.toThrow(/pending|review|outbox/i);
  request.mockImplementationOnce(async () => json(user.name)).mockImplementationOnce(async () => json({ status: 'Failure' }));
  await q.replay();
  expect((await q.list())[0].state).toBe('review');
  await expect(q.assertEmpty()).rejects.toThrow(/pending|review|outbox/i);
});

test('purge removes the current browser outbox only after it is empty', async () => {
  const q = await outbox();
  await q.capture(data, 100);
  expect(q.purge, 'there is no empty-only purge path').toBeTypeOf('function');
  await expect(q.purge()).rejects.toThrow(/pending|review|outbox/i);
  const other = await outbox({ user: { name: 'other@example.com', roles: ['Cashier'] } });
  await expect(other.purge()).rejects.toThrow(/pending|review|outbox/i);
  expect(await q.list()).toHaveLength(1);
});

test('offline capture fails closed without browser cross-tab locks', async () => {
  delete navigator.locks;
  const q = await outbox();
  await expect(q.capture(data, 100)).rejects.toThrow(/lock|browser|unsupported/i);
  expect(await q.list()).toEqual([]);
});

test('a browser print exception retains one durable order for reprint without duplicate recapture', async () => {
  print.mockImplementation(() => { throw new Error('Printer unavailable'); });
  const q = await outbox();
  const row = await q.capture(data, 100);
  expect(row.rounds[0].offline_printed).toBe(0);
  expect(await q.list()).toHaveLength(1);
});

test('replay is exclusive, exposes Replaying, and prohibits capture while it is running', async () => {
  const q = await outbox();
  await q.capture(data, 100);
  let release!: () => void;
  const normal = request.getMockImplementation()!;
  request.mockImplementationOnce(async () => { await new Promise<void>((r) => { release = r; }); return json(user.name); });
  const first = q.replay();
  await vi.waitFor(() => expect(q.replaying).toBe(true));
  await expect(q.capture(data, 100)).rejects.toThrow(/replaying/i);
  const second = q.replay();
  release();
  await Promise.all([first, second]);
  expect(q.replaying).toBe(false);
  expect(sends).toHaveLength(1);
  request.mockImplementation(normal);
});
