import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
import { useRootStore } from '../store/root-store';
import { usePOSStore } from '../store/pos-store';
import PaymentDialog from './PaymentDialog';

// Simphony charged/cash tip: the tip is entered at settlement, is part of the
// amount the tenders must cover (never change), and travels to make_invoice,
// where the server books it to the till's Tips Payable account.
const root = useRootStore.getState(), pos = usePOSStore.getState();
beforeEach(async () => { await initI18n('en'); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); useRootStore.setState(root, true); usePOSStore.setState(pos, true); });

function show(opts?: {
  paymentModes?: string[];
  paymentModeMeta?: Record<string, { type: string; overtender_rule: string }>;
  enableDiscount?: number;
  grandTotal?: number;
  roundedTotal?: number;
  chargedTip?: number;
}) {
  useRootStore.setState({ user: { name: 'user', roles: ['URY Cashier'] } });
  usePOSStore.setState({
    paymentModes: opts?.paymentModes ?? ['Cash'],
    fetchPaymentModes: async () => {},
    posProfile: {
      name: 'Till',
      customer: 'Walk-in',
      allow_partial_payment: 0,
      enable_discount: opts?.enableDiscount ?? 0,
      order_type_customer_map: {},
      default_customer: null,
      payment_mode_meta: opts?.paymentModeMeta,
    } as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
  });
  render(
    <PaymentDialog
      customer="Walk-in"
      grandTotal={opts?.grandTotal ?? 100}
      roundedTotal={opts?.roundedTotal ?? opts?.grandTotal ?? 100}
      order={{
        invoice_printed: 1,
        ...(opts?.chargedTip !== undefined ? { custom_charged_tip: opts.chargedTip } : {}),
      }}
      invoice="B1"
      posProfile="Till"
      table={null}
      cashier="user"
      owner="user"
      onClose={vi.fn()}
      fetchOrders={async () => {}}
      clearSelectedOrder={vi.fn()}
    />,
  );
}

const settlement = (post: { mock: { calls: unknown[][] } }) =>
  post.mock.calls.find(([method]) => String(method).endsWith('make_invoice'))?.[1] as Record<string, unknown> | undefined;

it('adds the tip to the amount due and sends it with the settlement', async () => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'Submitted' } });
  show();
  fireEvent.change(screen.getByRole('textbox', { name: /^tip$/i }), { target: { value: '20' } });
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } });
  expect(screen.getByRole('status')).toHaveTextContent(/Short by.*20/);
  expect(screen.getByRole('button', { name: /^Pay / })).toBeDisabled();

  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '120' } });
  expect(screen.queryByText(/Change due/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /^Pay / }));
  await waitFor(() => expect(settlement(post)).toEqual(expect.objectContaining({
    tip: 20, payments: [{ mode_of_payment: 'Cash', amount: 120 }],
  })));
});

it('settles without a tip as before', async () => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'Submitted' } });
  show();
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } });
  fireEvent.click(screen.getByRole('button', { name: /^Pay / }));
  await waitFor(() => expect(settlement(post)).toBeDefined());
  expect(settlement(post)?.tip ?? 0).toBe(0);
});

it('refuses a negative tip', () => {
  show();
  fireEvent.change(screen.getByRole('textbox', { name: /^tip$/i }), { target: { value: '-5' } });
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '100' } });
  expect(screen.getByRole('button', { name: /^Pay / })).toBeDisabled();
});

it('shows the Tip line and tip-inclusive Total in Order Summary when a tip is entered', () => {
  show();
  const summary = screen.getByText('Order Summary').parentElement!;
  expect(within(summary).queryByText(/^Tip$/)).toBeNull();
  expect(summary).toHaveTextContent(/Total.*100/);

  fireEvent.change(screen.getByRole('textbox', { name: /^tip$/i }), { target: { value: '20' } });
  expect(within(summary).getByText(/^Tip$/)).toBeInTheDocument();
  expect(summary).toHaveTextContent(/Tip.*20/);
  expect(summary).toHaveTextContent(/Total.*120/);
});

it('never shows Change due on non-cash over-tender for Refuse or Post to Charged Tip', () => {
  show({
    paymentModes: ['Maximus Cash', 'Maximus Card'],
    paymentModeMeta: {
      'Maximus Cash': { type: 'Cash', overtender_rule: 'Refuse' },
      'Maximus Card': { type: 'Bank', overtender_rule: 'Refuse' },
    },
  });
  const [cashRefuse, cardRefuse] = screen.getAllByRole('spinbutton');
  fireEvent.change(cardRefuse, { target: { value: '130' } });
  expect(screen.queryByText(/Change due/i)).toBeNull();
  expect(screen.getByText(/only cash gives change/i)).toHaveTextContent(/30/);

  // Cash over-tender still shows Change due.
  fireEvent.change(cardRefuse, { target: { value: '' } });
  fireEvent.change(cashRefuse, { target: { value: '130' } });
  expect(screen.getByText(/Change due/i)).toHaveTextContent(/30/);

  cleanup();
  show({
    paymentModes: ['Maximus Cash', 'Maximus Card'],
    paymentModeMeta: {
      'Maximus Cash': { type: 'Cash', overtender_rule: 'Refuse' },
      'Maximus Card': { type: 'Bank', overtender_rule: 'Post to Charged Tip' },
    },
  });
  const [, cardTip] = screen.getAllByRole('spinbutton');
  fireEvent.change(cardTip, { target: { value: '130' } });
  expect(screen.queryByText(/Change due/i)).toBeNull();
  expect(screen.getByText(/charged tip/i)).toHaveTextContent(/30/);

  cleanup();
  show({
    grandTotal: 120,
    roundedTotal: 120,
    paymentModes: ['Maximus Cash', 'Maximus Card'],
    paymentModeMeta: {
      'Maximus Cash': { type: 'Cash', overtender_rule: 'Refuse' },
      'Maximus Card': { type: 'Bank', overtender_rule: 'Refuse' },
    },
  });
  const [mixedCash, mixedCard] = screen.getAllByRole('spinbutton');
  fireEvent.change(mixedCash, { target: { value: '5' } });
  fireEvent.change(mixedCard, { target: { value: '130' } });
  expect(screen.queryByText(/Change due.*15/i)).toBeNull();
  expect(screen.getByText(/only cash gives change/i)).toHaveTextContent(/10/);
});

it('computes bill discount on pre-tip subtotal and autofills amount due including tip', async () => {
  show({ enableDiscount: 1, grandTotal: 120, roundedTotal: 120, chargedTip: 20 });
  const tipField = screen.getByRole('textbox', { name: /^tip$/i }) as HTMLInputElement;
  expect(tipField.value).toBe('20');

  fireEvent.change(screen.getByPlaceholderText('Enter %'), { target: { value: '10' } });
  fireEvent.click(screen.getByRole('button', { name: /^Apply$/i }));

  const summary = screen.getByText('Order Summary').parentElement!;
  await waitFor(() => expect(summary).toHaveTextContent(/Discount.*-.*10/));
  expect(summary).toHaveTextContent(/Subtotal.*100/);
  expect(summary).toHaveTextContent(/Tip.*20/);
  expect(summary).toHaveTextContent(/Total.*110/);

  const cashInput = screen.getAllByRole('spinbutton')[1] as HTMLInputElement;
  fireEvent.change(cashInput, { target: { value: '' } });
  fireEvent.focus(cashInput);
  expect(cashInput.value).toBe('110');
});
