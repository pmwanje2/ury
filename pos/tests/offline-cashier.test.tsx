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
import { PrinterWatchProvider } from '../src/components/PrinterWatch';
import PaymentDialog from '../src/components/PaymentDialog';
import { createPosClosingEntry, submitPosClosingEntry, createSubPosClosing, submitSubPosClosing } from '../src/lib/pos-closing-api';

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.spyOn(call, 'get').mockResolvedValue({ message: [] });
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
    selectedOrderType: 'Take Away', selectedCustomer: { id: 'CUST-A', name: 'Walk In' } as any, selectedTable: null, selectedRoom: null,
    paymentModes: ['Cash'], orderComment: '', activeOrders: [{ id: 'Tea', uniqueId: 'T1', name: 'Tea', price: 100, quantity: 1 } as any] });
  vi.spyOn(call, 'post').mockRejectedValue(new TypeError('Network Error'));
  vi.spyOn(window, 'open').mockImplementation(() => ({ document: { write() {}, close() {} }, focus() {}, print() {} }) as any);
  await act(async () => root.render(<OrderPanel />));
  await act(async () => { button(/^Add New Order$/).click(); await vi.waitFor(() => expect(usePOSStore.getState().activeOrders).toEqual([])); });
  expect(call.post).not.toHaveBeenCalled();
  expect(await new OfflineOutbox({ profile: usePOSStore.getState().posProfile!, user: useRootStore.getState().user! }).list()).toHaveLength(1);
});

test.each([false, true])('a held round stays in the cart without an offline ticket (browser online: %s)', async (online) => {
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: online });
  const profile = { ...usePOSStore.getState().posProfile!, ury_enable_hold_fire: 1 };
  const held = { id: 'Tea', uniqueId: 'T1', name: 'Tea', price: 100, quantity: 1, ury_held: 1 };
  usePOSStore.setState({ posProfile: profile, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: { id: 'CUST-A', name: 'Walk In', phone: '' },
    selectedTable: null, selectedRoom: null, paymentModes: ['Cash'], activeOrders: [held as any] });
  const post = vi.spyOn(call, 'post').mockRejectedValue({ httpStatus: 503 });
  const popup = vi.spyOn(window, 'open').mockReturnValue(null);
  const refused = vi.spyOn(showToast, 'error');
  const saved = vi.spyOn(showToast, 'success');
  await act(async () => root.render(<OrderPanel />));
  await act(async () => { button(/^Add New Order$/).click(); await vi.waitFor(() => expect(refused.mock.calls.length + saved.mock.calls.length).toBeGreaterThan(0)); });
  expect(post).toHaveBeenCalledTimes(online ? 1 : 0);
  expect(refused).toHaveBeenCalledWith(expect.stringMatching(/hold.*(online|offline)/i));
  expect(usePOSStore.getState().activeOrders).toEqual([held]);
  expect(await new OfflineOutbox({ profile, user: useRootStore.getState().user! }).list()).toEqual([]);
  expect(popup).not.toHaveBeenCalled();
});

test('pending outbox disables till close even when the browser is online', async () => {
  await queued(); Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Network Error'); }));
  await act(async () => root.render(<MemoryRouter><PrinterWatchProvider><Header /></PrinterWatchProvider></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  await vi.waitFor(() => expect(button(/Close Shift/i).disabled).toBe(true));
});

test('logout with a pending outbox warns and preserves queue and caches without sending logout', async () => {
  const q = await queued(); localStorage.setItem('pos_profile', 'cached');
  const request = vi.fn(async () => new Response('{}', { status: 200 })); vi.stubGlobal('fetch', request);
  const warning = vi.spyOn(showToast, 'error');
  await act(async () => root.render(<MemoryRouter><PrinterWatchProvider><Header /></PrinterWatchProvider></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  await act(async () => { button(/^Logout$/).click(); await vi.waitFor(() => expect(warning).toHaveBeenCalledWith(expect.stringMatching(/outbox|pending|review/i))); });
  expect(request).not.toHaveBeenCalled(); expect(await q.list()).toHaveLength(1);
  expect(localStorage.getItem('pos_profile')).toBe('cached');
});

test('till close is disabled offline in the real header', async () => {
  await act(async () => root.render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><PrinterWatchProvider><Header /></PrinterWatchProvider></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  expect(button(/Close Shift/i).disabled).toBe(true);
});

test('clear cache preserves pending orders and read caches', async () => {
  await queued(); localStorage.setItem('pos_profile', 'cached');
  const warning = vi.spyOn(showToast, 'error');
  await act(async () => root.render(<MemoryRouter><PrinterWatchProvider><Header /></PrinterWatchProvider></MemoryRouter>));
  await act(async () => button(/^Cashier/).click());
  await act(async () => { button(/^Clear Cache$/).click(); });
  await vi.waitFor(() => expect(warning).toHaveBeenCalledWith(expect.stringMatching(/outbox|pending|review/i)));
  expect(localStorage.getItem('pos_profile')).toBe('cached');
});

test('the cashier can submit a positive extra round to a queued order with a cumulative total', async () => {
  const q = await queued(); const row = (await q.list())[0];
  usePOSStore.setState({ isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: { id: 'CUST-A', name: 'Walk In' } as any, selectedTable: null, selectedRoom: null,
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
  await act(async () => root.render(<PaymentDialog order={{ invoice_printed: 1 }} onClose={vi.fn()} grandTotal={100} roundedTotal={100} invoice="ONLINE-1" customer="Walk In" posProfile="Till A" table={null} cashier="cashier@example.com" owner="cashier@example.com" fetchOrders={vi.fn()} clearSelectedOrder={vi.fn()} />));
  await act(async () => button(/^Pay /).click());
  expect(container.textContent).toMatch(/refused offline|not available offline/i);
  expect(post).not.toHaveBeenCalled();
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

test('a gateway loss after a full cash settlement queues the same invoice and id without refreshing live orders', async () => {
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  usePOSStore.setState({ paymentModes: ['Cash'], fetchPaymentModes: vi.fn() });
  const clientId = '22222222-2222-4222-8222-222222222222';
  const post = vi.spyOn(call, 'post').mockRejectedValue({ httpStatus: 502 });
  const refresh = vi.fn();
  await act(async () => root.render(<PaymentDialog order={{ invoice_printed: 1, ury_client_order_id: clientId }} onClose={vi.fn()} grandTotal={100} roundedTotal={100} invoice="INV-A" customer="Guest A" posProfile="Till A" table={null} cashier="cashier@example.com" owner="cashier@example.com" fetchOrders={refresh} clearSelectedOrder={vi.fn()} />));
  await act(async () => button(/^Pay /).click());
  const q = new OfflineOutbox({ profile: usePOSStore.getState().posProfile!, user: useRootStore.getState().user! });
  await vi.waitFor(async () => expect(await q.list()).toEqual([expect.objectContaining({ client_order_id: clientId, server_invoice: 'INV-A' })]));
  expect(post).toHaveBeenCalledWith(expect.stringContaining('make_invoice'), expect.objectContaining({client_order_id: clientId, submit_invoice: 1}));
  expect(refresh).not.toHaveBeenCalled();
});

test('opening screen logout keeps a pending queue and does not end the server session', async () => {
  const queue = await queued();
  vi.mocked(call.get).mockResolvedValue({ message: { company: 'Company A', allowed_profiles: [], payment_modes: [], permissions: {}, multi_cashier: {} } });
  const { auth } = await import('@ury/core');
  const signOut = vi.spyOn(auth, 'logout').mockResolvedValue(undefined);
  const warning = vi.spyOn(showToast, 'error');
  const { default: Opening } = await import('../src/components/POSOpeningScreen');
  await act(async () => root.render(<Opening />));
  await act(async () => button(/^Logout$/).click());
  await vi.waitFor(() => expect(warning).toHaveBeenCalledWith(expect.stringMatching(/outbox|pending|review/i)));
  expect(signOut).not.toHaveBeenCalled();
  expect(await queue.list()).toHaveLength(1);
});

test('applying a discount is refused offline before the total changes', async () => {
  usePOSStore.setState({ paymentModes: ['Cash'], fetchPaymentModes: vi.fn(), posProfile: { ...usePOSStore.getState().posProfile!, enable_discount: 1 } });
  await act(async () => root.render(<PaymentDialog order={{invoice_printed: 1}} onClose={vi.fn()} grandTotal={100} roundedTotal={100} invoice="INV-A" customer="Guest A" posProfile="Till A" table={null} cashier="Cashier" owner="Cashier" fetchOrders={vi.fn()} clearSelectedOrder={vi.fn()} />));
  await act(async () => button(/^Apply$/).click());
  expect(container.textContent).toMatch(/refused offline/i);
});

test('a server logout failure keeps the opening screen and confirmed opening for a retry', async () => {
  const key = 'ury_offline_opening:cashier@example.com';
  sessionStorage.setItem(key, 'confirmed');
  const { auth } = await import('@ury/core');
  vi.spyOn(auth, 'logout').mockRejectedValue(new Error('lost connection'));
  const { clearCashierStorage } = await import('../src/offline/cashier');
  await expect(clearCashierStorage(true)).rejects.toThrow();
  expect(sessionStorage.getItem(key)).toBe('confirmed');
});
