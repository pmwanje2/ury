import { setImmediate as flushPromises } from 'node:timers/promises';
import { createPinia, setActivePinia } from 'pinia';
import { expect, it, vi } from 'vitest';
import { useNotificationModal } from '../src/stores/NotificationModal.js';
import { useTableStore } from '../src/stores/Table.js';
import { usetoggleRecentOrder } from '../src/stores/recentOrder.js';
import { useInvoiceDataStore } from '../src/stores/invoiceData.js';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../src/router', () => ({ default: { push: vi.fn().mockResolvedValue(undefined) } }));
vi.mock('../src/stores/frappeSdk.js', () => ({ default: { call: () => api, db: () => ({}) } }));
vi.mock('../src/stores/Auth.js', () => ({ useAuthStore: () => ({ sessionUser: 'captain' }) }));
vi.mock('../src/stores/Notification.js', () => ({ useNotifications: () => ({ createNotification: vi.fn() }) }));

it.each(['table', 'recent order'])('reopens and submits every issued row from the %s', async (via) => {
  setActivePinia(createPinia());
  const table = useTableStore();
  const recent = usetoggleRecentOrder();
  const invoice = useInvoiceDataStore();
  const menu = table.menu;
  table.tableMenu = menu.items = [{ item: 'Soup', item_name: 'Soup', rate: 10 }];
  const rows = [
    { name: 'east-soup', item_code: 'Soup', qty: 1, rate: 10, reservation_line_key: 'east' },
    { name: 'west-soup', item_code: 'Soup', qty: 1, rate: 11, reservation_line_key: 'west' },
    { name: 'old-pie', item_code: 'Pie', qty: 1, rate: 5, comment: 'warm' },
    { name: 'other-pie', item_code: 'Pie', qty: 1, rate: 5, comment: 'cold' },
  ];
  api.get.mockResolvedValue({ message: { name: 'Check', waiter: 'captain', restaurant_table: 'T1',
    customer: 'Guest', no_of_pax: 1, items: rows } });
  vi.spyOn(table.customers, 'fectchCustomerFavouriteItem').mockImplementation(() => {});
  vi.spyOn(table, 'fetchTable').mockImplementation(() => {});
  if (via === 'table') {
    await table.loadSelectedTable({ name: 'T1' });
  } else {
    recent.invoiceNumber = 'Check';
    await recent.editOrder();
  }
  await flushPromises();
  expect(menu.cart.filter(row => row.item === 'Soup').reduce((sum, row) => sum + row.qty, 0)).toBe(2);
  expect(menu.cart).toHaveLength(rows.length);
  const expected = rows.map(({ item_code, ...row }) => expect.objectContaining({ item: item_code, ...row }));
  expect(menu.cart).toEqual(expected);
  api.post.mockResolvedValue({ message: { name: 'Check' } });
  await invoice.invoiceCreation();
  expect(api.post).toHaveBeenLastCalledWith(expect.stringMatching(/\.sync_order$/),
    expect.objectContaining({ invoice: 'Check', items: expected }));
});

it('keeps only the latest recent-order load when responses overlap', async () => {
  setActivePinia(createPinia());
  const recent = usetoggleRecentOrder();
  recent.invoiceNumber = 'Check';
  let finishFirst;
  let finishLast;
  api.get.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { finishLast = resolve; }));
  const first = recent.editOrder();
  const last = recent.editOrder();
  const response = comment => ({ message: { name: 'Check', custom_comments: comment,
    items: [{ name: 'pie-row', item_code: 'Pie', qty: 1, rate: 10 }] } });
  finishLast(response('latest'));
  await last;
  await flushPromises();
  finishFirst(response('stale'));
  await first;
  await flushPromises();
  expect(recent.menu.cart.map(row => row.name)).toEqual(['pie-row']);
  expect(recent.menu.comments).toBe('latest');
});

it('keeps the latest split check when two floor table loads resolve out of order', async () => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  const table = { name: 'T1', occupied: 1 };
  const rows = ['east-soup', 'west-pie', 'west-soup'].map(name => ({
    name, item_code: name.endsWith('soup') ? 'Soup' : 'Pie', qty: 1, rate: 10,
  }));
  const response = comment => ({ message: {
    name: 'Check', waiter: 'captain', custom_comments: comment, items: rows,
  } });
  let first, second;
  api.get.mockImplementationOnce(() => new Promise(resolve => { first = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { second = resolve; }));

  floor.routeToMenu(table);
  floor.routeToMenu(table);
  second(response('latest'));
  await flushPromises();
  first(response('stale'));
  await flushPromises();
  expect(floor.menu.cart.map(row => row.name)).toEqual(rows.map(row => row.name));
  expect(floor.menu.comments).toBe('latest');
});

function pendingFloorInvoice() {
  let resolve;
  api.get.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  return (table, dish) => resolve({ message: {
    name: 'Check-' + table, restaurant_table: table, waiter: 'captain', customer: 'Guest', no_of_pax: 1,
    items: [{ name: table + '-row', item_code: dish, qty: 1, rate: 10 }],
  } });
}

it.each([1, 2])('retains the pending menu addition in the cart and sync_order after %i same-table loads', async loads => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  vi.spyOn(floor.customers, 'fectchCustomerFavouriteItem').mockImplementation(() => {});
  vi.spyOn(floor, 'fetchTable').mockImplementation(() => {});
  const table = { name: 'T1', occupied: 1 };
  const responses = [];
  for (let i = 0; i < loads; i++) {
    responses.push(pendingFloorInvoice());
    floor.routeToMenu(table);
  }
  floor.menu.addToCart({ item: 'Pie', item_name: 'Pie', rate: 5 });
  for (const respond of responses.reverse()) {
    respond('T1', 'Soup');
    await flushPromises();
  }

  const lines = [expect.objectContaining({ name: 'T1-row', item: 'Soup', qty: 1 }),
    expect.objectContaining({ item: 'Pie', qty: 1, rate: 5 })];
  expect.soft(floor.menu.cart).toEqual(lines);
  api.post.mockResolvedValueOnce({ message: { name: 'Check-T1' } });
  await useInvoiceDataStore().invoiceCreation();
  expect(api.post).toHaveBeenLastCalledWith(expect.stringMatching(/\.sync_order$/),
    expect.objectContaining({ table: 'T1', invoice: 'Check-T1', items: lines }));
});

it('discards the first table’s local and saved rows when the cashier confirms a switch mid-load', async () => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  vi.spyOn(floor.customers, 'fectchCustomerFavouriteItem').mockImplementation(() => {});
  const first = pendingFloorInvoice();
  const second = pendingFloorInvoice();
  floor.routeToMenu({ name: 'T1', occupied: 1 });
  floor.menu.addToCart({ item: 'Pie', rate: 5 });
  floor.routeToMenu({ name: 'T2', occupied: 1 });
  const modal = useNotificationModal();
  expect(modal.isOpen).toBe(true);
  modal.handleConfirm();
  expect(floor.menu.cart).toEqual([]);
  floor.menu.addToCart({ item: 'Tea', rate: 3 });
  second('T2', 'Salad');
  await flushPromises();
  first('T1', 'Soup');
  await flushPromises();

  expect(floor.invoiceNo).toBe('Check-T2');
  expect(floor.menu.cart.map(row => row.item)).toEqual(['Salad', 'Tea']);
});

it('blocks Send until an occupied table has loaded its invoice and saved items', async () => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  const invoice = useInvoiceDataStore();
  vi.spyOn(floor.customers, 'fectchCustomerFavouriteItem').mockImplementation(() => {});
  vi.spyOn(floor, 'fetchTable').mockImplementation(() => {});
  api.post.mockClear().mockResolvedValue({ message: { name: 'Check-T1' } });
  const finish = pendingFloorInvoice();
  floor.routeToMenu({ name: 'T1', occupied: 1 });
  floor.customers.numberOfPax = 1;
  floor.menu.addToCart({ item: 'Pie', rate: 5 });
  await invoice.invoiceCreation();
  expect(api.post).not.toHaveBeenCalled();

  finish('T1', 'Soup');
  await flushPromises();
  await invoice.invoiceCreation();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(expect.stringMatching(/\.sync_order$/), expect.objectContaining({
    table: 'T1', invoice: 'Check-T1',
    items: [expect.objectContaining({ item: 'Soup', name: 'T1-row' }), expect.objectContaining({ item: 'Pie' })],
  }));
});

it.each(['rejected', 'missing invoice'])('blocks Send after a %s response for an occupied table', async outcome => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  vi.spyOn(floor, 'fetchTable').mockImplementation(() => {});
  api.post.mockClear().mockResolvedValue({ message: { name: 'Unexpected' } });
  if (outcome === 'rejected') api.get.mockRejectedValueOnce(new Error('Unavailable'));
  else api.get.mockResolvedValueOnce({ message: { items: [] } });
  floor.routeToMenu({ name: 'T1', occupied: 1 });
  await flushPromises();
  floor.customers.numberOfPax = 1;
  floor.menu.addToCart({ item: 'Pie', rate: 5 });
  await useInvoiceDataStore().invoiceCreation();
  expect(api.post).not.toHaveBeenCalled();
});

it('preserves note and covers edited during load and restores the next table’s own defaults', async () => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  vi.spyOn(floor.customers, 'fectchCustomerFavouriteItem').mockImplementation(() => {});
  const first = pendingFloorInvoice();
  floor.routeToMenu({ name: 'T1', occupied: 1 });
  floor.menu.comments = 'Bring water first';
  floor.customers.numberOfPax = 3;
  first('T1', 'Soup');
  await flushPromises();
  expect.soft(floor.menu.comments).toBe('Bring water first');
  expect.soft(floor.customers.numberOfPax).toBe(3);

  const second = pendingFloorInvoice();
  floor.routeToMenu({ name: 'T2', occupied: 1 });
  expect.soft(floor.menu.comments || '').toBe('');
  expect.soft(floor.customers.numberOfPax || '').toBe('');
  second('T2', 'Salad');
  await flushPromises();
  expect(floor.menu.comments || '').toBe('');
  expect(floor.customers.numberOfPax).toBe(1);
});

it('can start a new check after loading a vacant table with no saved invoice', async () => {
  setActivePinia(createPinia());
  const floor = useTableStore();
  vi.spyOn(floor, 'fetchTable').mockImplementation(() => {});
  api.get.mockResolvedValueOnce({ message: { items: [] } });
  api.post.mockClear().mockResolvedValueOnce({ message: { name: 'New-check' } });
  floor.routeToMenu({ name: 'T2', occupied: 0 });
  await flushPromises();
  floor.customers.numberOfPax = 2;
  floor.menu.addToCart({ item: 'Tea', rate: 3 });
  await useInvoiceDataStore().invoiceCreation();
  expect(api.post).toHaveBeenCalledWith(expect.stringMatching(/\.sync_order$/), expect.objectContaining({
    invoice: null, table: 'T2', items: [expect.objectContaining({ item: 'Tea' })],
  }));
});
