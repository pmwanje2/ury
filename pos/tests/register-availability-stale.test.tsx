import { act } from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
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

test.each([null, 900])('repeat uses the current price and promotion (%s)', async promo => {
  const current = {...usePOSStore.getState().menuItems[0], price: 1200, promo_rate: promo, promo_label: promo ? 'Lunch' : null};
  const previous = {...current, price: 800, promo_rate: 700, promo_label: 'Breakfast', quantity: 2};
  usePOSStore.setState({orderId: 'Check A', menuItems: [current]});
  await act(async () => root.render(<POS lastRound={{invoice: 'Check A', lines: [previous]}} />));
  await act(async () => screen.getByRole('button', {name: 'Repeat round'}).click());
  expect(usePOSStore.getState().activeOrders[0]).toMatchObject({
    price: 1200, promo_rate: promo, promo_label: current.promo_label, quantity: 2,
  });
});

test('a separate Open Item starts at one after a multiplied PLU', async () => {
  const regular = {...usePOSStore.getState().menuItems[0], item: '001'};
  usePOSStore.setState({menuItems: [regular, {...regular, item: '002', id: '002', price: 0, custom_is_open_priced: 1}]});
  await act(async () => root.render(<POS />));
  await act(async () => screen.getByRole('button', {name: 'PLU'}).click());
  const entry = container.querySelector('input[inputmode=numeric]')!;
  fireEvent.change(entry, {target: {value: '3×001'}});
  fireEvent.keyDown(entry, {key: 'Enter'});
  expect(usePOSStore.getState().activeOrders[0].quantity).toBe(3);
  await act(async () => screen.getByTestId('register-open-item-btn').click());
  expect(document.querySelector<HTMLInputElement>('#open-item-qty')?.value).toBe('1');
});

test.each(['All', 'Drinks'])('%s clears the current handheld category', async action => {
  vi.stubGlobal('innerWidth', 390);
  usePOSStore.setState({categories: [{name: 'Drinks', label: 'Drinks'}], selectedCategory: 'Drinks', quickFilter: 'all'});
  await act(async () => root.render(<POS />));
  await act(async () => within(container).getByRole('button', {name: action, exact: true}).click());
  expect(usePOSStore.getState().selectedCategory).toBe('');
});

test('repeat prices a linked condiment from its current parent option', async () => {
  const parent = {...usePOSStore.getState().menuItems[0], quantity: 1, reservationLineKey: 'base'};
  const option = {label: 'Extra', upcharge_amount: 0, option_code: 'extra', item: 'extra', price_mode: 'Menu Item Price' as const, unit_price: 50, available: true};
  const menu = {...parent, modifier_groups: [{name: 'Extras', display_title: 'Extras', group_type: 'Condiment' as const, required: false, prompt_on_add: false, min_selections: 0, max_selections: 1, allow_prefixes: false, allowed_prefixes: [], inherit_parent_routing: true, hide_unpriced_on_receipt: false, options: [option]}]};
  const child = {...parent, item: 'extra', isModifier: true, parentLineKey: 'base', reservationLineKey: 'old-extra',
    modifierGroup: 'Extras', modifierOption: 'extra', price: 10};
  usePOSStore.setState({orderId: 'Check A', menuItems: [menu]});
  await act(async () => root.render(<POS lastRound={{invoice: 'Check A', lines: [parent, child]}} />));
  await act(async () => screen.getByRole('button', {name: 'Repeat round'}).click());
  const [base, extra] = usePOSStore.getState().activeOrders;
  expect(extra).toMatchObject({price: 50, parentLineKey: base.reservationLineKey});
});
