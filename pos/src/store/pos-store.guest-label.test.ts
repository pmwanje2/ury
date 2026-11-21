import { beforeEach, expect, it, vi } from 'vitest';

// M03: the order's guest label lives in the till store next to its comment:
// set from the order when a table is opened, cleared with the cart.

vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: vi.fn(), post: vi.fn() },
  storage: { setItem: vi.fn(), getItem: vi.fn() },
  siteCurrencyCode: () => null,
  currencyDisplaySymbol: () => '',
}));
vi.mock('../lib/menu-api', () => ({
  getRestaurantMenu: vi.fn(async () => []),
  getAggregatorMenu: vi.fn(async () => []),
}));
vi.mock('../lib/pos-profile-api', () => ({ getCombinedPosProfile: vi.fn(), getCurrencyInfo: vi.fn() }));
vi.mock('../lib/menu-course-api', () => ({ getMenuCourses: vi.fn(async () => []) }));
vi.mock('../lib/customer-api', () => ({
  getCustomerGroups: vi.fn(async () => []),
  getCustomerTerritories: vi.fn(async () => []),
}));
vi.mock('../lib/payment-api', () => ({ getPaymentModes: vi.fn(async () => []) }));
const getTableOrderMock = vi.fn();
vi.mock('../lib/order-api', () => ({ getTableOrder: (...a: unknown[]) => getTableOrderMock(...a) }));

import { usePOSStore } from './pos-store';

type GuestLabelState = { guestLabel?: string; setGuestLabel?: (label: string) => void };
const guest = () => (usePOSStore.getState() as unknown as GuestLabelState);

const orderWithItems = (label: string | null) => ({
  message: {
    name: 'INV-1', customer: 'CUST', customer_name: 'Customer', mobile_number: '',
    custom_guest_label: label, items: [{ item_code: 'TEA', item_name: 'Tea', qty: 1, rate: 10, name: 'row-1' }],
  },
});

beforeEach(() => {
  getTableOrderMock.mockReset();
  usePOSStore.setState({ posProfile: { name: 'P' } as never, activeOrders: [], orderId: null });
  usePOSStore.setState({ guestLabel: 'stale' } as never);
});

it('setGuestLabel stores the label', () => {
  expect(typeof guest().setGuestLabel).toBe('function');
  guest().setGuestLabel!('Susan');
  expect(guest().guestLabel).toBe('Susan');
});

it('opening a table with an order loads that order\'s label', async () => {
  getTableOrderMock.mockResolvedValue(orderWithItems('Susan'));
  await usePOSStore.getState().loadTableOrder('T1');
  expect(guest().guestLabel).toBe('Susan');
});

it('opening an order without a label, or an empty table, clears it', async () => {
  getTableOrderMock.mockResolvedValue(orderWithItems(null));
  await usePOSStore.getState().loadTableOrder('T1');
  expect(guest().guestLabel).toBe('');
  usePOSStore.setState({ guestLabel: 'stale' } as never);
  getTableOrderMock.mockResolvedValue({ message: null });
  await usePOSStore.getState().loadTableOrder('T2');
  expect(guest().guestLabel).toBe('');
});

it('resetOrderState and clearTableOrder clear the label', () => {
  usePOSStore.getState().resetOrderState();
  expect(guest().guestLabel).toBe('');
  usePOSStore.setState({ guestLabel: 'stale' } as never);
  usePOSStore.getState().clearTableOrder();
  expect(guest().guestLabel).toBe('');
});
