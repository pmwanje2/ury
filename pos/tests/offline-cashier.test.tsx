import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call, db } from '@ury/core';
import { testLocks } from './offline-locks';
import { showToast } from '@ury/ui';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import OrderPanel from '../src/components/OrderPanel';
import { OfflineOutbox } from '../src/offline/outbox';
import Header from '../src/components/Header';
import POSOpeningScreen from '../src/components/POSOpeningScreen';
import POSOpeningProvider from '../src/components/POSOpeningProvider';
import PaymentDialog from '../src/components/PaymentDialog';
import { createPosClosingEntry, submitPosClosingEntry, createSubPosClosing, submitSubPosClosing } from '../src/lib/pos-closing-api';

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
  Object.defineProperty(navigator, 'locks', { configurable: true, value: testLocks() });
  await initI18n('en');
  document.cookie = 'user_id=cashier%40example.com';
  useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['Cashier'] } });
  usePOSStore.setState({ posProfile: { name: 'Till A', ury_offline_capture: 1,
    role_allowed_for_billing: [{ role: 'Cashier' }], cashier: 'cashier@example.com', owner: 'cashier@example.com' } as any });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true }); });

const button = (text: RegExp) => Array.from(document.querySelectorAll('button')).find((b) => text.test(b.textContent || ''))!;

async function queued() {
  const queue = new OfflineOutbox({ profile: usePOSStore.getState().posProfile!, user: useRootStore.getState().user!, print: () => true });
  await queue.capture({ pos_profile: 'Till A', customer: 'Walk In', items: [{ item: 'Tea', item_name: 'Tea', rate: 100, qty: 1 }],
    no_of_pax: 1, invoice: null, last_invoice: null, order_type: 'Take Away' }, 100);
  return queue;
}

test('the real cashier submit persists offline before clearing its cart without a live sync_order', async () => {
  usePOSStore.setState({ isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: { name: 'Walk In' } as any, selectedTable: null, selectedRoom: null,
    paymentModes: ['Cash'], orderComment: '', activeOrders: [{ id: 'Tea', uniqueId: 'T1', name: 'Tea', price: 100, quantity: 1 } as any] });
  vi.spyOn(call, 'post').mockRejectedValue(new TypeError('Network Error'));
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write() {}, close() {} }, focus() {}, print() {} }) as any);
  await act(async () => root.render(<OrderPanel />));
  await act(async () => { button(/^Add New Order$/).click(); await vi.waitFor(() => expect(usePOSStore.getState().activeOrders).toEqual([])); });
  expect(call.post).not.toHaveBeenCalled();
  expect(await new OfflineOutbox({ profile: usePOSStore.getState().posProfile!, user: useRootStore.getState().user! }).list()).toHaveLength(1);
});

test('pending outbox disables till close even when the browser is online', async () => {
  await queued(); Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Network Error'); }));
  await act(async () => root.render(<MemoryRouter><Header /></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  await vi.waitFor(() => expect(button(/Close Shift/i).disabled).toBe(true));
});

test('logout with a pending outbox warns and preserves queue and caches without sending logout', async () => {
  const q = await queued(); localStorage.setItem('pos_profile', 'cached');
  const request = vi.fn(async () => new Response('{}', { status: 200 })); vi.stubGlobal('fetch', request);
  const warning = vi.spyOn(showToast, 'error');
  await act(async () => root.render(<MemoryRouter><Header /></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  await act(async () => { button(/^Logout$/).click(); await vi.waitFor(() => expect(warning).toHaveBeenCalledWith(expect.stringMatching(/outbox|pending|review/i))); });
  expect(request).not.toHaveBeenCalled(); expect(await q.list()).toHaveLength(1);
  expect(localStorage.getItem('pos_profile')).toBe('cached');
});

test('till close is disabled offline in the real header', async () => {
  await act(async () => root.render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Header /></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  expect(button(/Close Shift/i).disabled).toBe(true);
});

test('clear cache preserves pending orders and read caches', async () => {
  await queued(); localStorage.setItem('pos_profile', 'cached');
  const warning = vi.spyOn(showToast, 'error');
  await act(async () => root.render(<MemoryRouter><Header /></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  await act(async () => { button(/^Clear Cache$/).click(); });
  await vi.waitFor(() => expect(warning).toHaveBeenCalledWith(expect.stringMatching(/outbox|pending|review/i)));
  expect(localStorage.getItem('pos_profile')).toBe('cached');
});

test.each(['screen', 'provider'])('the %s logout path refuses a pending queue and retains caches', async (component) => {
  await queued(); localStorage.setItem('pos_profile', 'cached');
  vi.spyOn(call, 'get').mockRejectedValue(new Error('No open session'));
  const request = vi.fn(async () => new Response('{}')); vi.stubGlobal('fetch', request);
  const warning = vi.spyOn(showToast, 'error');
  await act(async () => root.render(component === 'screen' ? <POSOpeningScreen /> : <POSOpeningProvider><div>POS</div></POSOpeningProvider>));
  await act(async () => { button(/^Logout$/).click(); });
  await vi.waitFor(() => expect(warning).toHaveBeenCalledWith(expect.stringMatching(/outbox|pending|review/i)));
  expect(request).not.toHaveBeenCalled();
  expect(localStorage.getItem('pos_profile')).toBe('cached');
});

test('the cashier can submit a positive extra round to a queued order with a cumulative total', async () => {
  const q = await queued(); const row = (await q.list())[0];
  usePOSStore.setState({ isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: { name: 'Walk In' } as any, selectedTable: null, selectedRoom: null,
    paymentModes: ['Cash'], activeOrders: [{ id: 'Tea', uniqueId: 'T2', name: 'Tea', price: 100, quantity: 1 } as any] });
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write() {}, close() {} }, focus() {}, print() {} }) as any);
  await act(async () => root.render(<OrderPanel />));
  let select: HTMLSelectElement | null = null;
  await vi.waitFor(async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); select = container.querySelector('[data-testid="offline-round-target"]'); expect(select?.options.length).toBe(2); });
  await act(async () => { select!.value = row.client_order_id; select!.dispatchEvent(new Event('change', { bubbles: true })); });
  await act(async () => { button(/^Add New Order$/).click(); await vi.waitFor(async () => expect((await q.list())[0].rounds).toHaveLength(2)); });
  const stored = (await q.list())[0];
  expect(stored.client_order_id).toBe(row.client_order_id);
  expect(stored.rounds[1].expected_total).toBe(200);
});

test('an extra round uses the captured customer even after the new-order cart has been reset', async () => {
  const q = await queued(); const row = (await q.list())[0];
  usePOSStore.setState({ isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: null, selectedTable: null, selectedRoom: null,
    paymentModes: ['Cash'], activeOrders: [{ id: 'Tea', uniqueId: 'T3', name: 'Tea', price: 100, quantity: 1 } as any] });
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write() {}, close() {} }, focus() {}, print() {} }) as any);
  await act(async () => root.render(<OrderPanel />));
  let select: HTMLSelectElement | null = null;
  await vi.waitFor(async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); select = container.querySelector('[data-testid="offline-round-target"]'); expect(select?.options.length).toBe(2); });
  await act(async () => { select!.value = row.client_order_id; select!.dispatchEvent(new Event('change', { bubbles: true })); });
  await act(async () => { button(/^Add New Order$/).click(); await vi.waitFor(async () => expect((await q.list())[0].rounds).toHaveLength(2)); });
  expect((await q.list())[0].order.customer).toBe('Walk In');
});

test('online invoice settlement and discount show an offline refusal instead of posting', async () => {
  usePOSStore.setState({ paymentModes: ['Cash'], fetchPaymentModes: vi.fn(), posProfile: { ...usePOSStore.getState().posProfile!, enable_discount: 1 } });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await act(async () => root.render(<PaymentDialog onClose={vi.fn()} grandTotal={100} roundedTotal={100} invoice="ONLINE-1" customer="Walk In" posProfile="Till A" table={null} cashier="cashier@example.com" owner="cashier@example.com" fetchOrders={vi.fn()} clearSelectedOrder={vi.fn()} />));
  await act(async () => button(/^Pay /).click());
  expect(container.textContent).toMatch(/refused offline|not available offline/i);
  expect(post).not.toHaveBeenCalled();
});

test('new offline orders use the profile native walk-in customer after cart reset without searching the server', async () => {
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!, customer: 'Native Walk In' },
    isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: null, selectedTable: null, selectedRoom: null,
    paymentModes: ['Cash'], activeOrders: [{ id: 'Tea', uniqueId: 'T4', name: 'Tea', price: 100, quantity: 1 } as any] });
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write() {}, close() {} }, focus() {}, print() {} }) as any);
  await act(async () => root.render(<OrderPanel />));
  await act(async () => { button(/^Add New Order$/).click(); });
  const rows = await new OfflineOutbox({ profile: usePOSStore.getState().posProfile!, user: useRootStore.getState().user! }).list();
  expect(rows).toHaveLength(1);
  expect(rows[0].order.customer).toBe('Native Walk In');
});

test.each([
  ['main draft', () => createPosClosingEntry({} as any)], ['main submit', () => submitPosClosingEntry('C1')],
  ['sub draft', () => createSubPosClosing({} as any)], ['sub submit', () => submitSubPosClosing('C1')],
])('%s cannot bypass offline close through its API wrapper', async (_name, close) => {
  vi.spyOn(db, 'createDoc').mockResolvedValue({ name: 'C1' });
  vi.spyOn(db, 'updateDoc').mockResolvedValue({ name: 'C1' });
  await expect(close()).rejects.toThrow(/offline|outbox/i);
  expect(db.createDoc).not.toHaveBeenCalled(); expect(db.updateDoc).not.toHaveBeenCalled();
});
