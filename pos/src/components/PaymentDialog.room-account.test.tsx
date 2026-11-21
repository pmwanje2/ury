// The Pay guard composes two rules: a Room Charge waits for a verified room
// (#114) and a short settlement is only for a named account (#109). Neither
// rule may drop the other when both are configured on the same till.
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import PaymentDialog from './PaymentDialog';
import { initI18n } from '../i18n';
import { useRootStore } from '../store/root-store';
import { usePOSStore } from '../store/pos-store';

vi.mock('../lib/print', () => ({ printOrder: vi.fn() }));

const METHOD = 'pms_bridge.room_charge_lookup';
let container: HTMLDivElement;
let root: Root;
const initialRoot = useRootStore.getState();
const initialStore = usePOSStore.getState();
type Profile = NonNullable<typeof initialStore.posProfile>;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  useRootStore.setState({user:{name:'cashier',roles:['URY Cashier']}});
  await initI18n('en');
  usePOSStore.setState({
    paymentModes: ['Room Charge'],
    fetchPaymentModes: async () => {},
    posProfile: {
      name: 'Till A', customer: 'Walk-in Customer', allow_partial_payment: 1,
      order_type_customer_map: { 'Dine In': { customer: 'Table Guest' } }, default_customer: null,
      room_lookup_method: METHOD,
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
const checkRoomButton = () => Array.from(document.querySelectorAll('button'))
  .find((button) => button.textContent === 'Check room');

async function render(customer: string, total = 100) {
  await act(async () => root.render(
    <PaymentDialog order={{ invoice_printed: 1 }} onClose={vi.fn()} grandTotal={total} roundedTotal={total}
      invoice="BILL-1" customer={customer} posProfile="Till A" table={null}
      cashier="cashier@example.com" owner="waiter@example.com"
      fetchOrders={vi.fn(async () => {})} clearSelectedOrder={vi.fn()} />,
  ));
}

function setValue(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function enterRoomCharge(amount: string) {
  const input = document.querySelectorAll<HTMLInputElement>('input[type="number"]')[0];
  await act(async () => setValue(input, amount));
}

async function enterRoom(room: string) {
  const box = document.querySelector<HTMLInputElement>('#room-charge-reference');
  expect(box, 'no room/folio box for the Room Charge tender').not.toBeNull();
  await act(async () => setValue(box!, room));
}

async function verifyRoom(room: string) {
  vi.spyOn(call, 'get').mockResolvedValue({ message: {
    room_number: room, guest_name: 'Susan K.', departure: '2026-11-16', no_post: 0,
  } });
  await enterRoom(room);
  await act(async () => checkRoomButton()!.click());
  await act(async () => {});
}

test('a named account short-settling on Room Charge keeps Pay disabled until the room is verified', async () => {
  const post = vi.spyOn(call, 'post');
  await render('House Account');
  await enterRoomCharge('40');
  await enterRoom('108');
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

test('a full Room Charge with an unverified room keeps Pay disabled', async () => {
  const post = vi.spyOn(call, 'post');
  await render('House Account');
  await enterRoomCharge('100');
  await enterRoom('108');
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

test('a default customer short-settling on a verified Room Charge keeps Pay disabled', async () => {
  const post = vi.spyOn(call, 'post');
  await render('Table Guest');
  await enterRoomCharge('40');
  await verifyRoom('108');
  expect(payButton().disabled).toBe(true);
  await act(async () => payButton().click());
  expect(post).not.toHaveBeenCalled();
});

test('a named account short-settles on a verified Room Charge and posts the room', async () => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render('House Account');
  await enterRoomCharge('40');
  await verifyRoom('108');
  expect(payButton().disabled).toBe(false);
  await act(async () => payButton().click());
  expect(post.mock.calls[0][1]).toMatchObject({
    customer: 'House Account', room_number: '108',
    payments: [{ mode_of_payment: 'Room Charge', amount: 40 }],
  });
});
