// Real @ury/core client, real order-api and pos-store, over real sockets.
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { AxiosInstance } from 'axios';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from 'vitest';
import { auth } from '../../packages/core/src/frappe/client';
import { usePOSStore } from '../src/store/pos-store';

const alice = 'alice@example.com';
const KNOWN = '22222222-2222-4222-8222-222222222222';
const OPEN_KEY = 'ury_open_table_checks:' + alice;
const transport = (auth as unknown as { axios: AxiosInstance }).axios;
let server: http.Server;
let base: string;
let status = 502;
let previous: { baseURL?: string; adapter?: unknown };

beforeAll(async () => {
  server = http.createServer((_request, response) => {
    response.statusCode = status;
    // nginx in front of a stopped backend answers with an HTML gateway page.
    response.setHeader('content-type', 'text/html');
    response.end('<html><h1>' + status + ' Bad Gateway</h1></html>');
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
  document.cookie = 'user_id=' + encodeURIComponent(alice) + '; Path=/';
  usePOSStore.setState({ error: null, orderLoading: false, selectedTable: null, activeOrders: [], isUpdatingOrder: false, orderId: null });
});
afterEach(() => {
  transport.defaults.baseURL = previous.baseURL;
  transport.defaults.adapter = previous.adapter as never;
  document.cookie = 'user_id=; Max-Age=0; Path=/';
});

test.each([502, 504])('selecting a table during a gateway %i keeps the till instead of a fatal error', async (code) => {
  status = code;
  sessionStorage.setItem(OPEN_KEY, JSON.stringify([{ table: 'T-01', clientOrderId: KNOWN, invoice: 'INV-1', grandTotal: 5000 }]));
  usePOSStore.setState({ activeOrders: [{ id: 'Tea', uniqueId: 'T1', name: 'Tea', price: 1000, quantity: 1 } as never] });
  usePOSStore.getState().setSelectedTable('T-01', 'Room');
  await expect.poll(() => usePOSStore.getState().orderLoading).toBe(false);
  const state = usePOSStore.getState();
  expect(state.error).toBeNull();
  expect(state.selectedTable).toBe('T-01');
  // Offline capture appends to the cached check; the cart must not pose as an online invoice.
  expect(state.isUpdatingOrder).toBe(false);
  expect(state.orderId).toBeNull();
  expect(JSON.parse(sessionStorage.getItem(OPEN_KEY) || '[]')).toEqual([
    expect.objectContaining({ table: 'T-01', clientOrderId: KNOWN }),
  ]);
});

