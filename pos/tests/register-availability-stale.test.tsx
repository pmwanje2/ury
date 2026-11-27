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

test('repeat rebuilds a remapped condiment using the current paired serving and unit price', async () => {
  const parent = {...usePOSStore.getState().menuItems[0], quantity: 1, reservationLineKey: 'base'};
  const option = {label: 'Extra pair', upcharge_amount: 0, option_code: 'extra', item: 'replacement', qty: 2, price_mode: 'Menu Item Price' as const, unit_price: 50, available: true};
  const menu = {...parent, modifier_groups: [{name: 'Extras', display_title: 'Extras', group_type: 'Condiment' as const, required: false, prompt_on_add: false, min_selections: 0, max_selections: 1, allow_prefixes: false, allowed_prefixes: [], inherit_parent_routing: true, hide_unpriced_on_receipt: false, options: [option]}]};
  const child = {...parent, item: 'extra', isModifier: true, parentLineKey: 'base', reservationLineKey: 'old-extra',
    modifierGroup: 'Extras', modifierOption: 'extra', price: 10};
  usePOSStore.setState({orderId: 'Check A', menuItems: [menu]});
  await act(async () => root.render(<POS lastRound={{invoice: 'Check A', lines: [parent, child]}} />));
  await act(async () => screen.getByRole('button', {name: 'Repeat round'}).click());
  const [base, extra] = usePOSStore.getState().activeOrders;
  expect(extra).toMatchObject({id: 'replacement', item: 'replacement', item_name: 'Extra pair', quantity: 2, price: 50, parentLineKey: base.reservationLineKey});
});

test.each([
  { oldFee: 0, fee: 2, available: true }, { oldFee: 1, fee: 2, available: true },
  { oldFee: 1, fee: 0, available: true }, { oldFee: 0, fee: 2, available: false },
])('repeat rebuilds a saved combo side when its fee changes from $oldFee to $fee (available: $available)', async ({ oldFee, fee, available }) => {
  const meal = { ...usePOSStore.getState().menuItems[0], id: 'meal', item: 'meal', price: 10, is_combo_meal: true };
  const choice = { group: 'Sides', slot_item: 'side', chosen_item: 'chips', option_code: 'CHIPS', label: 'Chips', qty: 1 };
  const parent = { ...meal, quantity: 1, lineRole: 'Combo Header' as const, reservationLineKey: 'old', bundleOverrides: [choice] };
  const option = { option_code: 'CHIPS', label: 'Chips', item: 'chips', replaces_bundle_item: 'side', available,
    price_mode: 'Combo Upcharge' as const, upcharge_item: 'side-fee', upcharge_amount: fee, unit_price: fee };
  const group = { name: 'Sides', display_title: 'Sides', group_type: 'Combo Choice' as const, required: true,
    prompt_on_add: true, min_selections: 1, max_selections: 1, allow_prefixes: false, allowed_prefixes: [],
    inherit_parent_routing: true, hide_unpriced_on_receipt: false, options: [option] };
  const oldChild = { ...meal, id: 'old-fee', item: 'old-fee', quantity: 1, price: oldFee, parentLineKey: 'old',
    isModifier: true, lineRole: 'Combo Upcharge' as const, modifierGroup: 'Sides', modifierOption: 'CHIPS' };
  usePOSStore.setState({ orderId: 'A', menuItems: [{ ...meal, modifier_groups: [group] }] });
  await act(async () => root.render(<POS lastRound={{ invoice: 'A', lines: oldFee ? [parent, oldChild] : [parent] }} />));
  await act(async () => screen.getByRole('button', { name: 'Repeat round' }).click());
  const lines = usePOSStore.getState().activeOrders;
  if (!available) {
    expect(lines).toHaveLength(0);
    expect(usePOSStore.getState().selectedItem?.item).toBe('meal');
    expect(await screen.findByRole('dialog')).toBeVisible();
    return;
  }
  expect(lines).toHaveLength(fee ? 2 : 1);
  expect(lines[0]).toMatchObject({ item: 'meal', price: 10, bundleOverrides: [expect.objectContaining(choice)] });
  if (fee) expect(lines[1]).toMatchObject({ item: 'side-fee', price: fee, quantity: 1,
    lineRole: 'Combo Upcharge', modifierOption: 'CHIPS', parentLineKey: lines[0].reservationLineKey });
});

test.each(['unavailable', 'removed', 'available'])('repeat checks every saved prep choice when one is %s', async state => {
  const tea = usePOSStore.getState().menuItems[0];
  const hot = { option_code: 'hot', label: 'Hot', price_mode: 'Unpriced Prep' as const,
    available: true, item: null, unit_price: 0, upcharge_amount: 0 };
  const group = { name: 'Prep', display_title: 'Preparation', group_type: 'Condiment' as const,
    required: false, prompt_on_add: false, min_selections: 0, max_selections: 2, allow_prefixes: false,
    allowed_prefixes: [], inherit_parent_routing: true, hide_unpriced_on_receipt: true,
    options: state === 'removed' ? [hot] : [hot, { ...hot, option_code: 'milk', label: 'With milk', available: state === 'available' }] };
  const parent = { ...tea, quantity: 2, selectedModifiers: [
    { group: 'Prep', option_code: 'hot', label: 'Hot' },
    { group: 'Prep', option_code: 'milk', label: 'Milk' },
  ] };
  usePOSStore.setState({ orderId: 'A', menuItems: [{ ...tea, modifier_groups: [group] }] });
  await act(async () => root.render(<POS lastRound={{ invoice: 'A', lines: [parent] }} />));
  await act(async () => screen.getByRole('button', { name: 'Repeat round' }).click());
  const lines = usePOSStore.getState().activeOrders;
  if (state === 'available') {
    expect(lines).toHaveLength(1);
    expect(lines[0].selectedModifiers?.[1]).toMatchObject({ option_code: 'milk', label: 'With milk', hide_on_receipt: 1 });
  } else {
    expect(lines).toHaveLength(0);
    expect(await screen.findByRole('dialog')).toBeVisible();
  }
});

test.each([
  { required: true, min: 0, max: 0, count: 0, valid: false },
  { required: false, min: 2, max: 0, count: 1, valid: false },
  { required: false, min: 0, max: 1, count: 2, valid: false },
  { required: true, min: 2, max: 2, count: 2, valid: true },
])('repeat enforces current prep group bounds ($required/$min/$max) for $count choices',
  async ({ required, min, max, count, valid }) => {
    const tea = usePOSStore.getState().menuItems[0];
    const options = ['Hot', 'Milk'].map(label => ({
      option_code: label, label, price_mode: 'Unpriced Prep' as const,
      available: true, item: null, unit_price: 0, upcharge_amount: 0,
    }));
    const group = { name: 'Prep', display_title: 'Preparation', group_type: 'Condiment' as const,
      required, min_selections: min, max_selections: max, prompt_on_add: false, allow_prefixes: false,
      allowed_prefixes: [], inherit_parent_routing: true, hide_unpriced_on_receipt: false, options };
    const parent = { ...tea, quantity: 3, selectedModifiers: options.slice(0, count).map(option => ({
      group: group.name, option_code: option.option_code, label: option.label,
    })) };
    usePOSStore.setState({ orderId: 'A', menuItems: [{ ...tea, modifier_groups: [group] }] });
    await act(async () => root.render(<POS lastRound={{ invoice: 'A', lines: [parent] }} />));
    await act(async () => screen.getByRole('button', { name: 'Repeat round' }).click());
    expect(usePOSStore.getState().activeOrders).toHaveLength(valid ? 1 : 0);
    if (!valid) expect(await screen.findByRole('dialog')).toBeVisible();
  });

async function startDrinkRound(twoPrompts = false) {
  const tea = usePOSStore.getState().menuItems[0];
  const coffee = { ...tea, id: 'coffee', item: 'coffee', name: 'Coffee', item_name: 'Coffee' };
  const cocoa = { ...tea, id: 'cocoa', item: 'cocoa', name: 'Cocoa', item_name: 'Cocoa' };
  const prep = { name: 'Prep', display_title: 'Preparation', group_type: 'Condiment' as const,
    required: true, min_selections: 1, max_selections: 1, prompt_on_add: true, allow_prefixes: false,
    allowed_prefixes: [], inherit_parent_routing: true, hide_unpriced_on_receipt: false,
    options: [{ option_code: 'hot', label: 'Hot', price_mode: 'Unpriced Prep' as const,
      available: true, item: null, unit_price: 0, upcharge_amount: 0 }] };
  const existing = { ...tea, item: 'cake', quantity: 1, uniqueId: 'existing-cake' };
  const drinks = twoPrompts ? [tea, coffee, cocoa] : [tea, coffee];
  usePOSStore.setState({ orderId: 'A', activeOrders: [existing],
    menuItems: drinks.map(drink => drink === tea ? tea : { ...drink, modifier_groups: [prep] }) });
  const lines = drinks.map((drink, i) => ({ ...drink, quantity: i + 1 }));
  await act(async () => root.render(<POS lastRound={{ invoice: 'A', lines }} />));
  await act(async () => screen.getByRole('button', { name: 'Repeat round' }).click());
  return existing;
}

async function confirmHot(drink: string) {
  const dialog = within(screen.getByRole('dialog'));
  expect(dialog.getByText(drink, { exact: true })).toBeVisible();
  await act(async () => dialog.getByRole('button', { name: /Hot/ }).click());
  await act(async () => dialog.getByRole('button', { name: /Add to order/i }).click());
}

test('repeat round adds Tea and the newly configured Coffee together', async () => {
  const existing = await startDrinkRound();
  expect(usePOSStore.getState().activeOrders).toEqual([existing]);
  await confirmHot('Coffee');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(usePOSStore.getState().activeOrders).toEqual([existing,
    expect.objectContaining({ item: 'tea', quantity: 1 }),
    expect.objectContaining({ item: 'coffee', quantity: 2, selectedModifiers: [expect.objectContaining({ option_code: 'hot' })] }),
  ]);
});

test.each(['confirm', 'cancel'] as const)('repeat round stages both drink configurations before final %s', async action => {
  const existing = await startDrinkRound(true);
  await confirmHot('Coffee');
  expect(usePOSStore.getState().activeOrders).toEqual([existing]);
  if (action === 'cancel') {
    await act(async () => within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }).click());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(usePOSStore.getState().activeOrders).toEqual([existing]);
    await act(async () => screen.getByRole('button', { name: 'Repeat round' }).click());
    expect(within(screen.getByRole('dialog')).getByText('Coffee', { exact: true })).toBeVisible();
    return;
  }
  await confirmHot('Cocoa');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(usePOSStore.getState().activeOrders).toEqual([existing,
    expect.objectContaining({ item: 'tea', quantity: 1 }),
    expect.objectContaining({ item: 'coffee', quantity: 2, selectedModifiers: [expect.objectContaining({ option_code: 'hot' })] }),
    expect.objectContaining({ item: 'cocoa', quantity: 3, selectedModifiers: [expect.objectContaining({ option_code: 'hot' })] }),
  ]);
});

test('repeat round can reconfigure a drink whose last prep group was removed', async () => {
  const tea = usePOSStore.getState().menuItems[0];
  const coffee = { ...tea, item: 'coffee', id: 'coffee', name: 'Coffee', item_name: 'Coffee' };
  usePOSStore.setState({ orderId: 'A', menuItems: [tea, coffee] });
  const lines = [{ ...tea, quantity: 1 }, { ...coffee, quantity: 2,
    selectedModifiers: [{ group: 'Old prep', option_code: 'hot', label: 'Hot' }] }];
  await act(async () => root.render(<POS lastRound={{ invoice: 'A', lines }} />));
  await act(async () => screen.getByRole('button', { name: 'Repeat round' }).click());
  const confirm = within(screen.getByRole('dialog')).getByRole('button', { name: /Add to order/i });
  expect(confirm).toBeEnabled();
  await act(async () => confirm.click());
  expect(usePOSStore.getState().activeOrders.map(row => [row.item, row.quantity])).toEqual([['tea', 1], ['coffee', 2]]);
});
