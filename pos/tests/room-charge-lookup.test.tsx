import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import PaymentDialog from '../src/components/PaymentDialog';
import { initI18n } from '../src/i18n';
import { usePOSStore } from '../src/store/pos-store';

// M02 / #49: an optional PMS room lookup on Room Charge. With
// room_lookup_method set on the POS Profile the cashier checks the room and
// sees who is in it before Pay; without it the till is unchanged.

let container: HTMLDivElement;
let root: Root;
const initialStore = usePOSStore.getState();
const METHOD = 'hospitality_kamra.room_charge_lookup';

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  await initI18n('en');
  usePOSStore.setState({
    paymentModes: ['Room Charge'],
    fetchPaymentModes: async () => {},
    posProfile: {
      name: 'Till A', customer: 'Walk-in Customer', allow_partial_payment: 0,
      room_lookup_method: METHOD,
    } as unknown as NonNullable<typeof initialStore.posProfile>,
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
  vi.unstubAllGlobals();
});

const buttonNamed = (pattern: RegExp) => Array.from(document.querySelectorAll('button'))
  .find((button) => pattern.test(button.textContent || ''));
const payButton = () => buttonNamed(/^Pay /)!;

async function render() {
  await act(async () => root.render(
    <PaymentDialog onClose={vi.fn()} grandTotal={100} roundedTotal={100}
      invoice="BILL-1" customer="Walk-in Customer" posProfile="Till A" table={null}
      cashier="cashier@example.com" owner="waiter@example.com"
      fetchOrders={vi.fn(async () => {})} clearSelectedOrder={vi.fn()} />,
  ));
}

async function setValue(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

const amount = () => document.querySelector<HTMLInputElement>('input[type="number"]')!;
const roomBox = () => document.querySelector<HTMLInputElement>('#room-charge-reference')!;

async function checkRoom() {
  await act(async () => buttonNamed(/^Check room$/)!.click());
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

test('configured lookup labels the section, checks the room and enables Pay on a match', async () => {
  const get = vi.spyOn(call, 'get').mockResolvedValue({ message: {
    room_number: '108', guest_name: 'Susan K.', departure: '2026-11-16', no_post: 0,
  } });
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  await render();
  await setValue(amount(), '100');
  expect(document.body.textContent).toContain('Room posting');
  await setValue(roomBox(), '108');
  expect(payButton().disabled).toBe(true);

  await checkRoom();
  expect(get).toHaveBeenCalledExactlyOnceWith(METHOD, { pos_profile: 'Till A', room: '108' });
  expect(document.body.textContent).toContain('Room 108 · Susan K. · departs 2026-11-16');
  expect(payButton().disabled).toBe(false);

  await act(async () => payButton().click());
  expect(post).toHaveBeenCalledWith('ury.ury.doctype.ury_order.ury_order.make_invoice',
    expect.objectContaining({ room_number: '108' }));
});

test('editing the room after a match disables Pay until it is checked again', async () => {
  vi.spyOn(call, 'get').mockResolvedValue({ message: {
    room_number: '108', guest_name: 'Susan K.', departure: '2026-11-16', no_post: 0,
  } });
  await render();
  await setValue(amount(), '100');
  await setValue(roomBox(), '108');
  await checkRoom();
  expect(payButton().disabled).toBe(false);
  await setValue(roomBox(), '109');
  expect(payButton().disabled).toBe(true);
  expect(document.body.textContent).not.toContain('Susan K.');
});

test('a No Post guest is shown as refused and Pay stays disabled', async () => {
  vi.spyOn(call, 'get').mockResolvedValue({ message: {
    room_number: '108', guest_name: 'Susan K.', departure: '2026-11-16', no_post: 1,
  } });
  await render();
  await setValue(amount(), '100');
  await setValue(roomBox(), '108');
  await checkRoom();
  expect(document.body.textContent).toContain("This guest's bill is closed to room charges");
  expect(payButton().disabled).toBe(true);
});

test('a refused lookup shows the server message and keeps Pay disabled', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(call, 'get').mockRejectedValue({ message: 'KMR-RC-004: no guest is checked in to room 999' });
  await render();
  await setValue(amount(), '100');
  await setValue(roomBox(), '999');
  await checkRoom();
  expect(document.body.textContent).toContain('KMR-RC-004: no guest is checked in to room 999');
  expect(payButton().disabled).toBe(true);
});

test('without a configured lookup the free-text room box and Pay are unchanged', async () => {
  usePOSStore.setState({ posProfile: { name: 'Till A', customer: 'Walk-in Customer' } as unknown as NonNullable<typeof initialStore.posProfile> });
  const get = vi.spyOn(call, 'get');
  await render();
  await setValue(amount(), '100');
  expect(document.body.textContent).toContain('Room / Folio');
  expect(buttonNamed(/^Check room$/)).toBeUndefined();
  await setValue(roomBox(), 'ROOM-101');
  expect(payButton().disabled).toBe(false);
  expect(get).not.toHaveBeenCalled();
});
