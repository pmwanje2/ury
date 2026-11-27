import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { testLocks } from './offline-locks';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import ScreenSizeProvider from '../src/components/ScreenSizeProvider';
import POS from '../src/pages/POS';

// The register page must hand the availability hook's "unconfirmed" state to
// its menu cards, or the "[N]?" indicator never reaches the cashier.

const tea = {
  item_code: 'tea', sellable: true, available_qty: 3, reason_code: 'AVAILABLE', production_policy: null,
  company: 'C1', branch: 'B1', department: null, production_unit: null, warehouse: null, plan_qty: null,
  plan_remaining: null, fg_available: null, max_producible: null, blocking_component: null, as_of: '',
};

vi.mock('../src/lib/use-menu-availability', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/use-menu-availability')>(),
  useMenuAvailability: () => ({ availabilityMap: { tea }, availabilityStale: true, refetchAll: async () => {} }),
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('innerWidth', 1280);
  vi.stubGlobal('innerHeight', 800);
  Object.defineProperty(navigator, 'locks', { configurable: true, value: testLocks() });
  await initI18n('en');
  useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['Cashier'] } });
  usePOSStore.setState({
    posProfile: { name: 'Till A', role_allowed_for_billing: [{ role: 'Cashier' }] } as never,
    isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Take Away', selectedCustomer: { name: 'Walk In' } as never, selectedTable: null,
    paymentModes: ['Cash'], orderComment: '', activeOrders: [],
    menuItems: [{ id: 'tea', item: 'tea', item_name: 'Tea', name: 'Tea', course: 'Drinks', price: 1000, image: null }] as never,
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test('the register marks a remaining count it cannot confirm', async () => {
  await act(async () => root.render(<ScreenSizeProvider><POS /></ScreenSizeProvider>));
  expect(container.textContent).toContain('3 left');
  expect(container.textContent).toContain('3 left?');
});

test.each([[10, 1], [0, 0]])('repeat round respects manual price entry at price %i', async (price, expected) => {
  const line = { ...usePOSStore.getState().menuItems[0], custom_allow_zero_price: 1, price, quantity: 1 };
  usePOSStore.setState({orderId: 'Check A', menuItems: [line], activeOrders: []});
  await act(async () => root.render(<POS lastRound={{invoice: 'Check A', lines: [line]}} />));
  const key = [...container.querySelectorAll('button')].find(button => button.textContent === 'Repeat round');
  expect(key).toBeDefined();
  await act(async () => key!.click());
  expect(usePOSStore.getState().activeOrders).toHaveLength(expected);
});
