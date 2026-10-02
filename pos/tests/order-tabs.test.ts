import assert from 'node:assert/strict';
import test from 'node:test';
import { resolve } from 'node:path';
import { create } from 'zustand';
import { environment, loadModule } from '../../packages/core/test/logout-harness.mjs';

const saved = JSON.stringify({
  tabOrder: [{ id: 'alice-tab', name: 'Tab 1' }], activeTabId: 'alice-tab',
  nextTabNumber: 2, heldTabs: {}, activeTabState: { activeOrders: [{ id: 'coffee', quantity: 2 }] },
});

function store(env: ReturnType<typeof environment>) {
  const core = loadModule(resolve('packages/core/src/storage.ts'), env.globals);
  return loadModule(resolve('pos/src/store/pos-store.ts'), env.globals, {
    zustand: { create }, uuid: { v4: () => 'new-tab' }, '@ury/core': core,
    '../data/order-types': { DEFAULT_ORDER_TYPE: 'Dine In' },
  }).usePOSStore;
}

test('order tabs restore only the authenticated user snapshot, never the legacy shared cart', () => {
  const env = environment();
  env.globals.localStorage.setItem('posOrderTabsData', saved);
  env.globals.localStorage.setItem('posOrderTabsData:alice@example.com', saved);
  env.globals.window.frappe.boot.user.name = 'bob@example.com';
  assert.equal(store(env).getState().activeOrders.length, 0);
});

test('order tabs restore the same user snapshot after a reload', () => {
  const env = environment();
  env.globals.localStorage.setItem('posOrderTabsData:alice@example.com', saved);
  assert.equal(store(env).getState().activeOrders[0]?.id, 'coffee');
});

test('order tabs persist under the current user, leaving other users untouched', () => {
  const env = environment();
  env.globals.localStorage.setItem('posOrderTabsData:alice@example.com', saved);
  env.globals.localStorage.setItem('posOrderTabsData:bob@example.com', 'Bob carts');
  store(env).setState({ isInitializing: false, activeOrders: [{ id: 'tea', quantity: 1 }] });
  const data = JSON.parse(env.globals.localStorage.getItem('posOrderTabsData:alice@example.com')!);
  assert.equal(data.activeTabState.activeOrders[0].id, 'tea');
  assert.equal(env.globals.localStorage.getItem('posOrderTabsData:bob@example.com'), 'Bob carts');
  assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), 'legacy carts');
});

test('an unidentified or Guest session neither restores nor saves another user cart', () => {
  for (const name of ['', 'Guest']) {
    const env = environment();
    env.globals.window.frappe.boot.user.name = name;
    env.globals.localStorage.setItem('posOrderTabsData', saved);
    const pos = store(env);
    assert.equal(pos.getState().activeOrders.length, 0);
    pos.setState({ isInitializing: false, activeOrders: [{ id: 'tea' }] });
    assert.equal(env.globals.localStorage.getItem('posOrderTabsData'), saved);
    assert.equal(env.globals.localStorage.getItem(`posOrderTabsData:${name}`), null);
  }
});

test('Outstanding is first while the existing history filters remain available', () => {
  const { getOrderStatusTypes } = loadModule(resolve('pos/src/data/order-types.ts'), {});
  assert.deepEqual(Array.from(getOrderStatusTypes(1, 30), (tab: { value: string }) => tab.value),
    ['Outstanding', 'Draft', 'Unbilled', 'Recently Paid', 'Paid', 'Consolidated', 'Return']);
});

test('the initial orders fetch and search both use Outstanding at the real API boundary', async () => {
  const env = environment();
  const calls: unknown[][] = [];
  const core = loadModule(resolve('packages/core/src/storage.ts'), env.globals);
  const api = loadModule(resolve('pos/src/lib/invoice-api.ts'), env.globals, {
    '@ury/core': { ...core, call: { get: async (...args: unknown[]) => {
      calls.push(args);
      return { message: { data: [], next: false } };
    } } },
  });
  const { createOrdersSlice } = loadModule(resolve('pos/src/store/slices/orders-slice.ts'), env.globals, {
    '@ury/core': core, '../../lib/invoice-api': api,
  });
  const orders = create(createOrdersSlice);
  assert.equal(orders.getState().selectedStatus, 'Outstanding');
  await orders.getState().fetchOrders();
  assert.equal(calls[0][0], 'ury.ury_pos.api.getPosInvoice');
  assert.equal((calls[0][1] as { status: string }).status, 'Outstanding');
  orders.getState().setOrderSearchQuery('POS-INV-1');
  await orders.getState().fetchOrders();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1])),
    ['ury.ury_pos.api.searchPosInvoice', { query: 'POS-INV-1', status: 'Outstanding' }]);
});
