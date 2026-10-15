import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { clearAppStorage } from '../../packages/core/src/storage';
import { testLocks } from './offline-locks';

const profile = { name: 'Till A', ury_offline_capture: 1, role_allowed_for_billing: [{ role: 'Cashier' }] };
const user = { name: 'cashier@example.com', roles: ['Cashier'] };
const KNOWN = '22222222-2222-4222-8222-222222222222';
const OPEN_KEY = `ury_open_table_checks:${user.name}`;

const json = (message: unknown, status = 200) => new Response(JSON.stringify({ message }), {
  status, headers: { 'Content-Type': 'application/json' },
});
const refuse = (message: string) => new Response(JSON.stringify({
  exc_type: 'ValidationError',
  exception: message,
  _server_messages: JSON.stringify([JSON.stringify({ message })]),
}), { status: 417, headers: { 'Content-Type': 'application/json' } });

function dine(table: string, item: { item: string; item_name: string; qty: number; rate: number }, customer = 'Walk In') {
  return {
    pos_profile: profile.name, customer, cashier: user.name, owner: user.name, waiter: user.name,
    items: [item], no_of_pax: 1, invoice: null, last_invoice: null, mode_of_payment: 'Cash',
    order_type: 'Dine In', table,
  };
}

// The server still owns the identity guard: a new client id cannot replace the
// open invoice already on that table. This mock refuses that, the same way
// sync_order does, and prices each append onto the running total.
function enforcingServer() {
  const invoices = new Map<string, { name: string; table?: string; client: string; docstatus: number; total: number; rounds: number }>();
  const tableOpen = new Map<string, string>();
  let seq = 0;
  return {
    invoices,
    request: async (url: string, init?: RequestInit) => {
      const method = String(url).split('/api/method/')[1]?.split('?')[0] || '';
      if (method === 'frappe.auth.get_logged_user') return json(user.name);
      const data = init?.body ? JSON.parse(String(init.body)) : {};
      if (method.endsWith('qz_print_update')) return json(true);
      if (method.endsWith('sync_order')) {
        const client = String(data.client_order_id || '');
        const table = data.table ? String(data.table) : undefined;
        let invoice = invoices.get(client);
        const openId = table ? tableOpen.get(table) : undefined;
        if (!invoice && openId && openId !== client) {
          const open = invoices.get(openId);
          if (open && open.docstatus === 0) return refuse('Client order ID cannot be changed for an existing order.');
        }
        if (!invoice) {
          seq += 1;
          invoice = { name: `INV-${seq}`, table, client, docstatus: 0, total: 0, rounds: 0 };
          invoices.set(client, invoice);
          if (table) tableOpen.set(table, client);
        }
        const added = (data.items || []).reduce((sum: number, item: { qty: number; rate: number }) => sum + item.qty * item.rate, 0);
        invoice.total += added;
        invoice.rounds += 1;
        if (data.expected_total != null && Number(data.expected_total) !== invoice.total) {
          return refuse(`URY_OFFLINE_TOTAL_MISMATCH: expected ${data.expected_total} server ${invoice.total}`);
        }
        return json({ name: invoice.name, docstatus: 0, ury_client_order_id: client });
      }
      if (method.endsWith('make_invoice')) {
        const invoice = invoices.get(String(data.client_order_id || ''));
        if (!invoice || invoice.docstatus !== 0) return refuse('This order is no longer open.');
        if (invoice.table && data.table && data.table !== invoice.table) return refuse('Client order ID does not match the requested table.');
        invoice.docstatus = 1;
        if (invoice.table && tableOpen.get(invoice.table) === invoice.client) tableOpen.delete(invoice.table);
        return json({ name: invoice.name, docstatus: 1 });
      }
      return json(true);
    },
  };
}

async function outbox(request?: typeof fetch) {
  const { OfflineOutbox } = await import('../src/offline/outbox');
  return new OfflineOutbox({ profile, user, request, print: async () => true });
}

beforeEach(() => {
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('navigator', { onLine: false, storage: { persist: vi.fn(async () => true) }, locks: testLocks() });
  sessionStorage.clear();
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test('the server identity guard is unchanged', () => {
  const source = readFileSync(resolve('ury/ury/doctype/ury_order/ury_order.py'), 'utf8');
  const guard = source.split('Client order ID cannot be changed for an existing order.');
  expect(guard.length - 1).toBeGreaterThanOrEqual(2);
});

test('20 offline orders on 2 tables with 2 settlements replay with no Needs review', async () => {
  const server = enforcingServer();
  const queue = await outbox(server.request);
  const menu = ['Tea', 'Coffee', 'Chips', 'Rice', 'Water'].map((name, index) => ({
    item: name, item_name: name, qty: 1, rate: (index + 1) * 1000,
  }));
  const tables = ['T-01', 'T-02'];
  for (let index = 0; index < 20; index += 1) {
    const item = menu[index % menu.length];
    const entry = await queue.capture(dine(tables[index % 2], item), item.rate);
    if (index < 2) {
      const stored = (await queue.list()).find((row) => row.client_order_id === entry.client_order_id)!;
      await queue.settle(entry.client_order_id, {
        kind: 'cash', mode_of_payment: 'Cash', amount: stored.rounds[stored.rounds.length - 1].expected_total,
      });
    }
  }
  const rows = await queue.list();
  const ids = [...new Set(rows.map((row) => row.client_order_id))];
  expect(ids, 'one open check per table, plus the two settled checks').toHaveLength(4);
  const rounds = rows.reduce((sum, row) => sum + row.rounds.length, 0);
  expect(rounds).toBe(20);
  await queue.replay();
  const left = await queue.list();
  expect(left.filter((row) => row.state === 'review')).toEqual([]);
  expect(left).toEqual([]);
  expect(server.invoices.size).toBe(4);
  const submitted = [...server.invoices.values()].filter((invoice) => invoice.docstatus === 1);
  const open = [...server.invoices.values()].filter((invoice) => invoice.docstatus === 0);
  expect(submitted).toHaveLength(2);
  expect(open).toHaveLength(2);
  expect([...server.invoices.values()].reduce((sum, invoice) => sum + invoice.rounds, 0)).toBe(20);
});

test('a server-known open check from the cache is appended, not given a new client id', async () => {
  sessionStorage.setItem(OPEN_KEY, JSON.stringify([{
    table: 'T-01', clientOrderId: KNOWN, invoice: 'INV-SERVER', grandTotal: 5000,
    customer: 'Walk In', orderType: 'Dine In', noOfPax: 2,
  }]));
  const server = enforcingServer();
  server.invoices.set(KNOWN, { name: 'INV-SERVER', table: 'T-01', client: KNOWN, docstatus: 0, total: 5000, rounds: 0 });
  const queue = await outbox(server.request);
  const entry = await queue.capture(dine('T-01', { item: 'Water', item_name: 'Water', qty: 1, rate: 1000 }, 'Other Guest'), 1000);
  expect(entry.client_order_id).toBe(KNOWN);
  expect(entry.server_invoice).toBe('INV-SERVER');
  expect(entry.order.customer).toBe('Walk In');
  expect(entry.rounds[0].expected_total).toBe(6000);
  await queue.replay();
  expect(await queue.list()).toEqual([]);
  expect(server.invoices.get(KNOWN)).toMatchObject({ name: 'INV-SERVER', total: 6000, rounds: 1, docstatus: 0 });
});

test('a settled offline check frees the table for a new client id', async () => {
  const queue = await outbox();
  const item = { item: 'Tea', item_name: 'Tea', qty: 1, rate: 1000 };
  const first = await queue.capture(dine('T-01', item), 1000);
  await queue.settle(first.client_order_id, { kind: 'cash', mode_of_payment: 'Cash', amount: 1000 });
  const second = await queue.capture(dine('T-01', { ...item, item: 'Coffee', item_name: 'Coffee', rate: 2000 }), 2000);
  expect(second.client_order_id).not.toBe(first.client_order_id);
  expect((await queue.list()).map((row) => row.client_order_id).sort()).toEqual([first.client_order_id, second.client_order_id].sort());
});

test('sign-out forgets cached open table checks', async () => {
  sessionStorage.setItem(OPEN_KEY, JSON.stringify([{ table: 'T-01', clientOrderId: KNOWN }]));
  await clearAppStorage();
  expect(sessionStorage.getItem(OPEN_KEY)).toBeNull();
});

// Sol #47 finding 3: the round id sent on the live write must be the one
// queued, so a lost acknowledgement replays into the server's round dedupe.
test.each(['cached server check', 'local offline check'])(
  'a gateway 504 on an occupied table (%s) queues the round id that was sent',
  async (source) => {
    const { initI18n } = await import('../src/i18n');
    await initI18n('en');
    vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write: vi.fn(), close: vi.fn() }, focus: vi.fn(), print: vi.fn(), opener: null }) as never);
    let open = KNOWN;
    if (source === 'cached server check') {
      sessionStorage.setItem(OPEN_KEY, JSON.stringify([{ table: 'T-01', clientOrderId: KNOWN, invoice: 'INV-SERVER', grandTotal: 5000, customer: 'Walk In', orderType: 'Dine In', noOfPax: 1 }]));
    } else {
      open = (await (await outbox()).capture(dine('T-01', { item: 'Tea', item_name: 'Tea', qty: 1, rate: 1000 }), 1000)).client_order_id;
    }
    vi.stubGlobal('navigator', { onLine: true, storage: { persist: vi.fn(async () => true) }, locks: testLocks() });
    const { cashierOutbox, submitOrderOfflineAware } = await import('../src/offline/cashier');
    cashierOutbox(profile, user).reachable = true;
    let sent: { clientOrderId: string; roundId: string } | undefined;
    const result = await submitOrderOfflineAware({
      profile, user, order: dine('T-01', { item: 'Water', item_name: 'Water', qty: 1, rate: 1000 }) as never, total: 1000,
      onlineSubmit: async (ids?: { clientOrderId: string; roundId: string }) => { sent = ids; throw { httpStatus: 504, message: 'timeout' }; },
    });
    expect(result.offline).toBe(true);
    if (!result.offline) return;
    expect(sent?.clientOrderId).toBe(open);
    expect(result.entry.client_order_id).toBe(open);
    expect(result.entry.rounds.at(-1)?.round_id).toBe(sent?.roundId);
  },
);

// Sol #47 finding 4: a partly replayed, unsettled check still owns its table.
test('partial replay then another outage appends to the same check instead of minting a second id', async () => {
  const { initI18n } = await import('../src/i18n');
  await initI18n('en');
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write: vi.fn(), close: vi.fn() }, focus: vi.fn(), print: vi.fn(), opener: null }) as never);
  const server = enforcingServer();
  let syncCalls = 0;
  const lossy = async (url: string, init?: RequestInit) => {
    if (String(url).endsWith('sync_order') && ++syncCalls === 2) {
      return new Response('<html>502 Bad Gateway</html>', { status: 502, headers: { 'Content-Type': 'text/html' } });
    }
    return server.request(url, init);
  };
  const queue = await outbox(lossy as typeof fetch);
  const first = await queue.capture(dine('T-01', { item: 'Tea', item_name: 'Tea', qty: 1, rate: 1000 }), 1000);
  await queue.capture(dine('T-01', { item: 'Coffee', item_name: 'Coffee', qty: 1, rate: 2000 }), 2000);
  await queue.replay();
  const partial = await queue.list();
  expect(partial).toHaveLength(1);
  expect(partial[0].next_round).toBe(1);

  const third = await queue.capture(dine('T-01', { item: 'Chips', item_name: 'Chips', qty: 1, rate: 3000 }), 3000);
  expect(third.client_order_id).toBe(first.client_order_id);

  vi.stubGlobal('navigator', { onLine: true, storage: { persist: vi.fn(async () => true) }, locks: testLocks() });
  const { cashierOutbox, submitOrderOfflineAware } = await import('../src/offline/cashier');
  cashierOutbox(profile, user).reachable = true;
  let sent: { clientOrderId: string; roundId: string } | undefined;
  const fourth = await submitOrderOfflineAware({
    profile, user, order: dine('T-01', { item: 'Water', item_name: 'Water', qty: 1, rate: 1000 }) as never, total: 1000,
    onlineSubmit: async (ids?: { clientOrderId: string; roundId: string }) => { sent = ids; throw { httpStatus: 502, message: 'gateway' }; },
  });
  expect(sent?.clientOrderId).toBe(first.client_order_id);
  expect(fourth.offline && fourth.entry.client_order_id).toBe(first.client_order_id);

  const rows = await queue.list();
  expect(rows.map((row) => row.client_order_id)).toEqual([first.client_order_id]);
  expect(rows[0].rounds.map((round) => round.expected_total)).toEqual([1000, 3000, 6000, 7000]);
  await queue.replay();
  expect(await queue.list()).toEqual([]);
  expect(server.invoices.size).toBe(1);
  expect(server.invoices.get(first.client_order_id)).toMatchObject({ total: 7000, rounds: 4, docstatus: 0 });
});
