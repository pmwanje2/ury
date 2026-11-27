import { setImmediate as flushPromises } from 'node:timers/promises';
import { createPinia, setActivePinia } from 'pinia';
import { expect, it, vi } from 'vitest';
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
