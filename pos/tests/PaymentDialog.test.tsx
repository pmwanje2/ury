import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import PaymentDialog from '../src/components/PaymentDialog';
import { initI18n } from '../src/i18n';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import { OfflineOutbox } from '../src/offline/outbox';
import { testLocks } from './offline-locks';

let container: HTMLDivElement;
let root: Root;
const onClose = vi.fn();
const clearSelectedOrder = vi.fn();
const fetchOrders = vi.fn(async () => {});
const initialStore = usePOSStore.getState();

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  await initI18n('en');
  usePOSStore.setState({
    paymentModes: ['Cash', 'Card'],
    fetchPaymentModes: async () => {},
    posProfile: {
      name: 'Till A', customer: 'Walk-in Customer', allow_partial_payment: 1,
    } as NonNullable<typeof initialStore.posProfile>,
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  usePOSStore.setState(initialStore, true);
  vi.restoreAllMocks();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
});

const payButton = () => Array.from(document.querySelectorAll('button'))
  .find((button) => /^Pay /.test(button.textContent || ''))!;

async function render(customer = 'House Account', total = 100) {
  await act(async () => root.render(
    <PaymentDialog onClose={onClose} grandTotal={total} roundedTotal={total}
      invoice="BILL-1" customer={customer} posProfile="Till A" table={null}
      cashier="cashier@example.com" owner="waiter@example.com"
      fetchOrders={fetchOrders} clearSelectedOrder={clearSelectedOrder} />,
  ));
}

async function enter(amount: string, index = 0) {
  const input = document.querySelectorAll<HTMLInputElement>('input[type="number"]')[index];
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, amount);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function enterText(value: string, selector = 'input[type="text"]') {
  const input = document.querySelector<HTMLInputElement>(selector)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

test.each(['40', '0', ''])('a named account can settle with tender %j and leave the rest owed', async (amount) => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render();
  await enter(amount);
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(post).toHaveBeenCalledExactlyOnceWith('ury.ury.doctype.ury_order.ury_order.make_invoice', {
    additionalDiscount: null, cashier: 'cashier@example.com', customer: 'House Account',
    invoice: 'BILL-1', owner: 'waiter@example.com',
    payments: [{ mode_of_payment: 'Cash', amount: amount === '40' ? 40 : 0 }],
    pos_profile: 'Till A', table: null,
  });
  expect(onClose).toHaveBeenCalledOnce();
  expect(clearSelectedOrder).toHaveBeenCalledOnce();
  expect(fetchOrders).toHaveBeenCalledOnce();
});

test('room charge asks for a raw room or folio reference and sends it with settlement', async () => {
  usePOSStore.setState({ paymentModes: ['Room Charge'] });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render('Walk-in Customer');
  expect(document.querySelector('input[type="text"]')).toBeNull();

  await enter('100');
  expect(document.body.textContent).toContain('Room / Folio');
  await enterText('  ROOM-101  ');
  await act(async () => payButton().click());

  expect(post).toHaveBeenCalledExactlyOnceWith('ury.ury.doctype.ury_order.ury_order.make_invoice', {
    additionalDiscount: null, cashier: 'cashier@example.com', customer: 'Walk-in Customer',
    invoice: 'BILL-1', kamra_ref: '  ROOM-101  ', owner: 'waiter@example.com',
    payments: [{ mode_of_payment: 'Room Charge', amount: 100 }],
    pos_profile: 'Till A', table: null,
  });
});

test.each([
  ['partial payments disabled', { allow_partial_payment: 0 }, 'House Account'],
  ['partial-payment setting missing', { allow_partial_payment: undefined }, 'House Account'],
  ['non-binary partial-payment setting', { allow_partial_payment: 2 }, 'House Account'],
  ['string partial-payment setting', { allow_partial_payment: '1' }, 'House Account'],
  ['default customer', {}, 'Walk-in Customer'],
  ['empty customer', {}, ''],
  ['whitespace customer', {}, '   '],
  ['unknown default customer', { customer: null }, 'House Account'],
  ['another profile in the store', { name: 'Till B' }, 'House Account'],
] as const)('short payment stays disabled for %s', async (_reason, fields, customer) => {
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!, ...fields } as unknown as NonNullable<typeof initialStore.posProfile> });
  const post = vi.spyOn(call, 'post');
  await render(customer);
  await enter('40');
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

test('short payment stays disabled before the profile is loaded', async () => {
  usePOSStore.setState({ posProfile: null });
  await render();
  await enter('40');
  expect(payButton().disabled).toBe(true);
});

test('unpaid account settlement uses an existing mode id with zero paid amount', async () => {
  usePOSStore.setState({ paymentModes: [{ id: 'Card', name: 'Bank card' }] as unknown as string[] });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render();
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(post.mock.calls[0][1]).toMatchObject({
    payments: [{ mode_of_payment: 'Card', amount: 0 }],
  });
});

test('an account cannot settle without a configured payment mode', async () => {
  usePOSStore.setState({ paymentModes: [] });
  const post = vi.spyOn(call, 'post');
  await render();
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

test.each([0, 1])('full split payment still works with partial-payment setting %s', async (flag) => {
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!, allow_partial_payment: flag } });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render('Walk-in Customer');
  await enter('40');
  expect(payButton().disabled).toBe(true);
  await enter('60', 1);
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(post.mock.calls[0][1]).toMatchObject({ payments: [
    { mode_of_payment: 'Cash', amount: 40 }, { mode_of_payment: 'Card', amount: 60 },
  ] });
});

test('overpayment still shows change and sends the full tender', async () => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render('Walk-in Customer');
  await enter('120');
  expect(payButton().disabled).toBe(false);
  expect(document.body.textContent).toMatch(/Change due:.*20/);
  await act(async () => payButton().click());
  expect(post.mock.calls[0][1]).toMatchObject({ payments: [{ mode_of_payment: 'Cash', amount: 120 }] });
});

test.each([['99.994', true], ['99.996', false]] as const)(
  'walk-in tender %s retains the existing shortfall tolerance', async (amount, disabled) => {
    await render('Walk-in Customer');
    await enter(amount);
    expect(payButton().disabled).toBe(disabled);
  },
);

test('a zero-total bill without tender retains the existing full-payment rule', async () => {
  await render('House Account', 0);
  expect(payButton().disabled).toBe(true);
});

test('a pending on-account settlement cannot be submitted twice', async () => {
  let finish!: (value: unknown) => void;
  const post = vi.spyOn(call, 'post').mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  await render();
  await enter('40');
  const button = payButton();
  expect(button.disabled).toBe(false);
  await act(async () => button.click());
  expect(button.disabled).toBe(true);
  await act(async () => button.click());
  expect(post).toHaveBeenCalledOnce();
  await act(async () => finish({ message: { docstatus: 1 } }));
});

test('O4 still refuses on-account settlement offline without posting', async () => {
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
  const post = vi.spyOn(call, 'post');
  vi.spyOn(console, 'error').mockImplementation(() => {});
  await render();
  await enter('40');
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(document.body.textContent).toMatch(/refused offline|not available offline/i);
  expect(post).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(clearSelectedOrder).not.toHaveBeenCalled();
});

test('a gateway 502 on an opted-in named-account short settlement is an error, not queued offline', async () => {
  vi.stubGlobal('indexedDB', new IDBFactory());
  Object.defineProperty(navigator, 'locks', { configurable: true, value: testLocks() });
  const user = { name: 'cashier@example.com', full_name: 'Cashier', roles: ['Cashier'] };
  useRootStore.setState({ user });
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!, ury_offline_capture: 1,
    role_allowed_for_billing: [{ role: 'Cashier' }] } as unknown as NonNullable<typeof initialStore.posProfile> });
  const post = vi.spyOn(call, 'post').mockRejectedValue({ httpStatus: 502, message: 'bad gateway' });
  vi.spyOn(console, 'error').mockImplementation(() => {});
  await render();
  await enter('40');
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  await vi.waitFor(() => expect(document.body.textContent).not.toMatch(/Processing/));
  expect(post).toHaveBeenCalledOnce();
  expect(onClose).not.toHaveBeenCalled();
  expect(clearSelectedOrder).not.toHaveBeenCalled();
  expect(await new OfflineOutbox({ profile: usePOSStore.getState().posProfile!, user }).list()).toEqual([]);
  useRootStore.setState({ user: null });
});
