// Ported from fork pos/tests/PaymentDialog.test.tsx (#44: 06f1100f, 2dac7691,
// 967956a7, 46345746). Offline-outbox cases belong to T5 and are not ported here.
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import PaymentDialog from './PaymentDialog';
import { initI18n } from '../i18n';
import { useRootStore } from '../store/root-store';
import { usePOSStore } from '../store/pos-store';

vi.mock('../lib/print', () => ({ printOrder: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
const onClose = vi.fn();
const clearSelectedOrder = vi.fn();
const fetchOrders = vi.fn(async () => {});
const initialRoot = useRootStore.getState();
const initialStore = usePOSStore.getState();
type Profile = NonNullable<typeof initialStore.posProfile>;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  useRootStore.setState({user:{name:'cashier',roles:['URY Cashier']}});
  await initI18n('en');
  usePOSStore.setState({
    paymentModes: ['Cash', 'Card'],
    fetchPaymentModes: async () => {},
    posProfile: {
      name: 'Till A', customer: 'Walk-in Customer', allow_partial_payment: 1,
      order_type_customer_map: {}, default_customer: null,
    } as unknown as Profile,
  });
  vi.spyOn(console, 'error').mockImplementation(() => {});
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  usePOSStore.setState(initialStore, true);
  useRootStore.setState(initialRoot, true);
  vi.restoreAllMocks();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

const payButton = () => Array.from(document.querySelectorAll('button'))
  .find((button) => /^Pay |^Processing/.test(button.textContent || ''))!;

async function render(customer = 'House Account', total = 100) {
  await act(async () => root.render(
    <PaymentDialog order={{ invoice_printed: 1 }} onClose={onClose} grandTotal={total} roundedTotal={total}
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

test.each(['40', '0', ''])('a named account can settle with tender %j and leave the rest owed', async (amount) => {
  vi.spyOn(call, 'get').mockResolvedValue({ message: { efris_status: null, fdn: null } });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render();
  await enter(amount);
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(post).toHaveBeenNthCalledWith(1, 'ury.ury.doctype.ury_order.ury_order.make_invoice', {
    cashier: 'cashier@example.com', customer: 'House Account',
    invoice: 'BILL-1', owner: 'waiter@example.com',
    payments: [{ mode_of_payment: 'Cash', amount: amount === '40' ? 40 : 0 }],
    pos_profile: 'Till A', table: null, submit_invoice: 0,
  });
  expect(post).toHaveBeenNthCalledWith(2, 'ury.ury.doctype.ury_order.ury_order.submit_order_invoice', {
    invoice: 'BILL-1',
  });
  expect(post).toHaveBeenCalledTimes(2);
  expect(onClose).toHaveBeenCalledOnce();
  expect(clearSelectedOrder).toHaveBeenCalledOnce();
  expect(fetchOrders).toHaveBeenCalledOnce();
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
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!, ...fields } as unknown as Profile });
  const post = vi.spyOn(call, 'post');
  await render(customer);
  await enter('40');
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

// v16 resolves a default customer per order type (custom_order_type_customers)
// and at profile level; neither is a named account, whatever the order type.
test.each([
  ['an order-type default customer', {
    order_type_customer_map: { 'Dine In': { customer: 'Table Guest' }, 'Take Away': { customer: 'Counter Guest' } },
  }, 'Table Guest'],
  ['another order type\'s default customer', {
    order_type_customer_map: { 'Dine In': { customer: 'Table Guest' }, 'Take Away': { customer: 'Counter Guest' } },
  }, 'Counter Guest'],
  ['the profile-level default customer', { default_customer: { customer: 'Profile Guest' } }, 'Profile Guest'],
  ['a profile without the default-customer keys', {
    order_type_customer_map: undefined, default_customer: undefined,
  }, 'House Account'],
] as const)('short payment stays disabled for %s', async (_reason, fields, customer) => {
  const profile: Record<string, unknown> = { ...usePOSStore.getState().posProfile!, ...fields };
  for (const key of Object.keys(fields)) if (profile[key] === undefined) delete profile[key];
  usePOSStore.setState({ posProfile: profile as unknown as Profile });
  const post = vi.spyOn(call, 'post');
  await render(customer);
  await enter('0');
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

test('a named account still settles short when order-type defaults are configured', async () => {
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!,
    order_type_customer_map: { 'Dine In': { customer: 'Table Guest' } },
    default_customer: { customer: 'Profile Guest' } } as unknown as Profile });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render('House Account');
  await enter('40');
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(post.mock.calls[0][1]).toMatchObject({
    customer: 'House Account', payments: [{ mode_of_payment: 'Cash', amount: 40 }],
  });
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
  usePOSStore.setState({ posProfile: { ...usePOSStore.getState().posProfile!, allow_partial_payment: flag } as unknown as Profile });
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

test.each([['99.994', true], ['99.996', false]] as const)(
  'walk-in tender %s retains the existing shortfall tolerance', async (amount, disabled) => {
    await render('Walk-in Customer');
    await enter(amount);
    expect(payButton().disabled).toBe(disabled);
  },
);

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
