import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { testLocks } from './offline-locks';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import OrderPanel from '../src/components/OrderPanel';
import ScreenSizeProvider from '../src/components/ScreenSizeProvider';

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
  Object.defineProperty(navigator, 'locks', { configurable: true, value: testLocks() });
  await initI18n('en');
  useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['Cashier'] } });
  usePOSStore.setState({
    posProfile: { name: 'Till A', ury_offline_capture: 1, role_allowed_for_billing: [{ role: 'Cashier' }] } as never,
    isInitializing: false, isUpdatingOrder: false, orderId: null, orderLoading: false,
    selectedOrderType: 'Dine In', selectedCustomer: { name: 'Walk In' } as never, selectedTable: 'T-01', selectedRoom: 'Room',
    paymentModes: ['Cash'], orderComment: '',
    activeOrders: [{ id: 'Tea', uniqueId: 'T1', name: 'Tea', price: 1000, quantity: 1 } as never],
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

test.each([
  [1280, 800],
  [412, 800],
])('Add New Order stays on screen at %ix%i instead of the desktop wall or a viewport-tall panel', async (width, height) => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height });
  await act(async () => root.render(
    <ScreenSizeProvider>
      <OrderPanel />
    </ScreenSizeProvider>,
  ));
  expect(container.textContent).not.toContain('Desktop Only');
  expect(container.textContent).toContain('Add New Order');
  const panel = container.querySelector('[data-testid="order-panel"]');
  expect(panel, 'the order panel must be a bounded till column').toBeTruthy();
  expect(panel?.className || '').not.toMatch(/100vh/);
  expect(panel?.className || '').toMatch(/inset-y-0|h-full/);
  const button = Array.from(container.querySelectorAll('button')).find((node) => node.textContent === 'Add New Order');
  expect(button).toBeTruthy();
});
