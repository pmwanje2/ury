// Real @ury/core client, order-api, pos-store and logout purge over real sockets.
// A table read issued for one signed-in user must never land in another
// user's open-check cache, or in the same user's cache after a purge.
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { AxiosInstance } from 'axios';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from 'vitest';
import { auth } from '../../packages/core/src/frappe/client';
import { clearAppStorage } from '../../packages/core/src/storage';
import { usePOSStore } from '../src/store/pos-store';

const alice = 'alice@example.com';
const bob = 'bob@example.com';
const KNOWN = '22222222-2222-4222-8222-222222222222';
const transport = (auth as unknown as { axios: AxiosInstance }).axios;
let server: http.Server;
let base: string;
let previous: { baseURL?: string; adapter?: unknown };
let arrived: () => void = () => undefined;
let release: () => void = () => undefined;

const aliceInvoice = {
  name: 'INV-ALICE', customer: 'ALICE-PRIVATE-CUSTOMER', customer_name: 'Alice Guest', order_type: 'Dine In',
  grand_total: 100, no_of_pax: 1, ury_client_order_id: KNOWN,
  items: [{ item_code: 'Tea', item_name: 'Tea', rate: 100, qty: 1, amount: 100 }],
};

beforeAll(async () => {
  server = http.createServer((_request, response) => {
    arrived();
    release = () => {
      response.statusCode = 200;
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify({ message: aliceInvoice }));
    };
  });
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
  base = 'http://127.0.0.1:' + (server.address() as AddressInfo).port;
});
afterAll(async () => { await new Promise<void>((done) => server.close(() => done())); });

beforeEach(() => {
  previous = { baseURL: transport.defaults.baseURL, adapter: transport.defaults.adapter };
  transport.defaults.baseURL = base;
  transport.defaults.adapter = 'fetch';
  sessionStorage.clear();
  usePOSStore.setState({ error: null, orderLoading: false });
});
afterEach(() => {
  transport.defaults.baseURL = previous.baseURL;
  transport.defaults.adapter = previous.adapter as never;
  document.cookie = 'user_id=; Max-Age=0; Path=/';
});

async function aliceReadAcross(purgeThen: () => void) {
  document.cookie = 'user_id=' + encodeURIComponent(alice) + '; Path=/';
  const inFlight = new Promise<void>((done) => { arrived = done; });
  const load = usePOSStore.getState().loadTableOrder('T-01');
  await inFlight;
  await clearAppStorage();
  purgeThen();
  release();
  await load;
}

test('an in-flight read for Alice is not cached as Bob\'s open check after logout', async () => {
  await aliceReadAcross(() => { document.cookie = 'user_id=' + encodeURIComponent(bob) + '; Path=/'; });
  expect(sessionStorage.getItem('ury_open_table_checks:' + bob)).toBeNull();
  expect(sessionStorage.getItem('ury_open_table_checks:' + alice)).toBeNull();
});

test('a read issued before a purge is dropped even for the same user', async () => {
  await aliceReadAcross(() => undefined);
  expect(sessionStorage.getItem('ury_open_table_checks:' + alice)).toBeNull();
});

test('a read with no purge still remembers the server-known open check', async () => {
  document.cookie = 'user_id=' + encodeURIComponent(alice) + '; Path=/';
  const inFlight = new Promise<void>((done) => { arrived = done; });
  const load = usePOSStore.getState().loadTableOrder('T-01');
  await inFlight;
  release();
  await load;
  expect(JSON.parse(sessionStorage.getItem('ury_open_table_checks:' + alice) || '[]')).toEqual([
    expect.objectContaining({ table: 'T-01', clientOrderId: KNOWN, invoice: 'INV-ALICE' }),
  ]);
});

// Sol #47 re-review: the stale response must not reach the active till either.
const bobCart = [{ id: 'Coffee', uniqueId: 'C1', name: 'Coffee', price: 2000, quantity: 1 }];
const bobCustomer = { id: 'BOB-GUEST', name: 'Bob Guest' };

function bobTill() {
  usePOSStore.setState({
    activeOrders: bobCart as never, selectedCustomer: bobCustomer as never, selectedTable: 'T-01',
    orderId: null, isUpdatingOrder: false, tableOrder: null, noOfPax: 2, orderComment: 'bob note',
  });
}

function expectBobTill() {
  const state = usePOSStore.getState();
  expect(state.orderId).toBeNull();
  expect(state.isUpdatingOrder).toBe(false);
  expect(state.tableOrder).toBeNull();
  expect(state.selectedCustomer).toEqual(bobCustomer);
  expect(state.activeOrders.map((item) => item.name)).toEqual(['Coffee']);
  expect(state.noOfPax).toBe(2);
  expect(state.orderComment).toBe('bob note');
  expect(state.error).toBeNull();
  expect(state.orderLoading).toBe(false);
}

test('Alice\'s late table read does not change Bob\'s active cart or customer', async () => {
  await aliceReadAcross(() => {
    document.cookie = 'user_id=' + encodeURIComponent(bob) + '; Path=/';
    bobTill();
  });
  expectBobTill();
});

test('a table read from before a same-user purge does not restore the old till', async () => {
  await aliceReadAcross(() => bobTill());
  expectBobTill();
});


