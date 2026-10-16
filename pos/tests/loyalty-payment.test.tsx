import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { call } from '@ury/core';
import PaymentDialog from '../src/components/PaymentDialog';
import { initI18n } from '../src/i18n';
import { usePOSStore } from '../src/store/pos-store';

const initial = usePOSStore.getState();
let root: Root;
let container: HTMLDivElement;
let quote: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
const onClose = vi.fn();
const profile = () => ({ name: 'Till A', customer: 'Walk-in Customer', ury_enable_loyalty: 1,
  allow_partial_payment: 0 }) as unknown as NonNullable<typeof initial.posProfile>;
const pay = () => Array.from(document.querySelectorAll('button')).find(b => /^Pay /.test(b.textContent || ''))!;
const redeem = () => Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Redeem');

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  await initI18n('en');
  usePOSStore.setState({ posProfile: profile(), paymentModes: ['Cash', 'Card'], fetchPaymentModes: async () => {} });
  quote = vi.spyOn(call, 'get').mockResolvedValue({ message: { enabled: true, balance: 50,
    loyalty_points: 0, loyalty_amount: 0, amount_due: 100 } });
  post = vi.spyOn(call, 'post').mockResolvedValue({ message: { docstatus: 1 } });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  usePOSStore.setState(initial, true);
  vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals();
});

async function render(customer = 'Named Customer', invoice = 'BILL-1') {
  await act(async () => root.render(<PaymentDialog onClose={onClose} grandTotal={100} roundedTotal={100}
    invoice={invoice} customer={customer} posProfile="Till A" table={null} cashier="cashier" owner="waiter"
    fetchOrders={async () => {}} clearSelectedOrder={() => {}} />));
}

async function enter(selector: string, value: string) {
  const input = document.querySelector<HTMLInputElement>(selector)!;
  expect(input, `Missing input ${selector}`).not.toBeNull();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

test('named customer sees native balance and redeem input, with no rates editable at the till', async () => {
  await render();
  expect(document.body.textContent).toContain('50 points available');
  expect(document.querySelector('input[aria-label="Points to redeem"]')).not.toBeNull();
  expect(document.body.textContent).not.toMatch(/conversion factor|collection factor/i);
});

test.each(['off', 'missing', 'default', 'walkin', 'profile mismatch', 'offline'])(
  'loyalty stays hidden and makes no balance request for %s', async reason => {
    const next = { ...profile() } as NonNullable<typeof initial.posProfile> & { ury_enable_loyalty?: number };
    if (reason === 'off') next.ury_enable_loyalty = 0;
    if (reason === 'missing') delete next.ury_enable_loyalty;
    if (reason === 'profile mismatch') next.name = 'Till B';
    if (reason === 'offline') Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
    usePOSStore.setState({ posProfile: next });
    await render(reason === 'default' ? 'Walk-in Customer' : reason === 'walkin' ? 'Walk-in' : 'Named Customer');
    expect(redeem()).toBeUndefined();
    expect(quote).not.toHaveBeenCalled();
  },
);

test('server quote, not browser conversion, reduces tender and sends points only', async () => {
  await render();
  await enter('input[aria-label="Points to redeem"]', '10');
  quote.mockResolvedValueOnce({ message: { enabled: true, balance: 50, loyalty_points: 10,
    loyalty_amount: 23, amount_due: 77 } });
  await act(async () => redeem()!.click());
  expect(document.body.textContent).toMatch(/77/);
  expect(document.body.textContent).not.toContain('Adjustment');
  await enter('input[type="number"]:not([aria-label="Points to redeem"])', '77');
  expect(pay().disabled).toBe(false);
  await act(async () => pay().click());
  expect(post.mock.calls[0][1]).toMatchObject({ loyalty_points: 10,
    payments: [{ mode_of_payment: 'Cash', amount: 77 }] });
  expect(post.mock.calls[0][1]).not.toHaveProperty('loyalty_amount');
  expect(post.mock.calls[0][1]).not.toHaveProperty('conversion_factor');
});

test('editing points invalidates the applied quote and disables payment until revalidated', async () => {
  await render();
  await enter('input[aria-label="Points to redeem"]', '10');
  expect(pay().disabled).toBe(true);
  quote.mockResolvedValueOnce({ message: { enabled: true, balance: 50, loyalty_points: 10,
    loyalty_amount: 20, amount_due: 80 } });
  await act(async () => redeem()!.click());
  await enter('input[aria-label="Points to redeem"]', '11');
  expect(pay().disabled).toBe(true);
});

test('stale points refused by settlement leave the dialog open and never show success', async () => {
  await render();
  await enter('input[aria-label="Points to redeem"]', '10');
  quote.mockResolvedValueOnce({ message: { enabled: true, balance: 50, loyalty_points: 10,
    loyalty_amount: 20, amount_due: 80 } });
  await act(async () => redeem()!.click());
  await enter('input[type="number"]:not([aria-label="Points to redeem"])', '80');
  post.mockRejectedValueOnce(new Error("You don't have enough Loyalty Points to redeem"));
  vi.spyOn(console, 'error').mockImplementation(() => {});
  await act(async () => pay().click());
  expect(document.body.textContent).toContain("You don't have enough Loyalty Points");
  expect(onClose).not.toHaveBeenCalled();
});

test('a quote response for an old bill cannot populate the next bill', async () => {
  let finish!: (result: unknown) => void;
  quote.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  await render();
  expect(quote).toHaveBeenCalledOnce();
  quote.mockResolvedValueOnce({ message: { enabled: false } });
  await render('Other Customer', 'BILL-2');
  await act(async () => finish({ message: { enabled: true, balance: 999, amount_due: 1 } }));
  expect(document.body.textContent).not.toContain('999 points available');
  expect(redeem()).toBeUndefined();
});
