import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

function show() {
  useRootStore.setState({ user: { name: 'user', roles: ['URY Cashier'] } });
  usePOSStore.setState({
    paymentModes: ['Cash'], fetchPaymentModes: async () => {},
    posProfile: { name: 'Till', customer: 'Walk-in', allow_partial_payment: 0, order_type_customer_map: {}, default_customer: null } as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
  });
  render(<PaymentDialog customer="Walk-in" grandTotal={100} roundedTotal={100} order={{ invoice_printed: 1 }} invoice="B1"
    posProfile="Till" table={null} cashier="user" owner="user" onClose={vi.fn()} fetchOrders={async () => {}} clearSelectedOrder={vi.fn()} />);
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
