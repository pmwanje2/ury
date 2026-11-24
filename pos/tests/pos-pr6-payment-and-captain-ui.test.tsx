import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mockUsePOSStore = vi.fn();
const mockUseRootStore = vi.fn();
const mockCallPost = vi.fn();
const mockCallGet = vi.fn();
const mockSubmitOrderOfflineAware = vi.fn();

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (selector?: (state: any) => unknown) => {
    const state = mockUsePOSStore();
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: any) => unknown) => {
    const state = mockUseRootStore();
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('../src/lib/print', () => ({
  printOrder: vi.fn().mockResolvedValue('browser'),
}));

vi.mock('../src/offline/cashier', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/offline/cashier')>();
  return {
    ...actual,
    submitOrderOfflineAware: (...args: unknown[]) => mockSubmitOrderOfflineAware(...args),
  };
});

vi.mock('@ury/core', () => ({
  formatCurrency: (amount: number, code?: string) => `${code || 'UGX'} ${Number(amount).toFixed(code === 'UGX' ? 0 : 2)}`,
  parseFrappeError: vi.fn((err: any, fallback?: string) => err?.message || fallback || 'Error'),
  printWithQz: vi.fn(),
  call: {
    post: (...args: unknown[]) => mockCallPost(...args),
    get: (...args: unknown[]) => mockCallGet(...args),
  },
}));

vi.mock('@ury/ui', () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Input: ({ ...props }: any) => <input {...props} />,
  Dialog: ({ children, open }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div data-testid="dialog-content">{children}</div>,
  Spinner: ({ message }: any) => <div>{message}</div>,
  Textarea: ({ ...props }: any) => <textarea {...props} />,
  cn: (...args: any[]) => args.filter(Boolean).join(' '),
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

describe('POS-PR-6 UI RED/GREEN: PaymentDialog G25 reference & G22 FX tender + SettledBills display', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCallPost.mockResolvedValue({ message: { name: 'POS-INV-001', status: 'Submitted' } });
    mockCallGet.mockResolvedValue({ message: {} });
    mockUseRootStore.mockReturnValue({
      user: { name: 'cashier@hotel.test', roles: ['URY Cashier'] },
    });
    mockUsePOSStore.mockReturnValue({
      paymentModes: ['Cash', 'MTN MoMo', 'USD Cash'],
      paymentModeDetails: {
        Cash: {
          mode_of_payment: 'Cash',
          type: 'Cash',
          custom_reference_mode: 'Hidden',
          custom_tender_currency: '',
        },
        'MTN MoMo': {
          mode_of_payment: 'MTN MoMo',
          type: 'Bank',
          custom_reference_mode: 'Required',
          custom_reference_label: 'MoMo Txn ID',
          custom_reference_placeholder: 'e.g. MM-2026-001',
          custom_reference_pattern: '^[A-Z0-9-]{6,20}$',
          custom_reference_date_required: 0,
          custom_tender_currency: '',
        },
        'USD Cash': {
          mode_of_payment: 'USD Cash',
          type: 'Cash',
          custom_reference_mode: 'Hidden',
          custom_tender_currency: 'USD',
          custom_fx_rate_type: 'Buying',
          exchange_rate: 3700,
          exchange_rate_date: '2026-11-24',
          fx_rate_status: 'fresh',
        },
      },
      fetchPaymentModes: vi.fn(),
      currency: 'UGX',
      posProfile: {
        name: 'Main Till',
        currency: 'UGX',
        default_payment_mode: 'Cash',
        enable_payment_without_printing: 1,
        custom_allow_fx_rate_override: 0,
      },
    });
  });

  it('G25: PaymentDialog renders config-driven reference_no input, locks Pay until valid pattern, and forwards reference_no', async () => {
    const { default: PaymentDialog } = await import('../src/components/PaymentDialog');

    render(
      <PaymentDialog
        onClose={vi.fn()}
        grandTotal={50000}
        roundedTotal={50000}
        invoice="POS-INV-001"
        customer="Walk-in Guest"
        posProfile="Main Till"
        table={null}
        cashier="cashier@hotel.test"
        owner="cashier@hotel.test"
        fetchOrders={vi.fn().mockResolvedValue(undefined)}
        clearSelectedOrder={vi.fn()}
      />,
    );

    const momoAmountInput = screen.getByTestId('payment-amount-MTN MoMo');
    fireEvent.change(momoAmountInput, { target: { value: '50000' } });

    const refInput = screen.getByTestId('payment-ref-MTN MoMo') as HTMLInputElement;
    expect(refInput).toBeInTheDocument();
    expect(refInput.placeholder).toBe('e.g. MM-2026-001');

    const payBtn = screen.getByTestId('payment-submit-btn') as HTMLButtonElement;
    expect(payBtn.disabled).toBe(true);

    // Invalid pattern keeps Pay disabled
    fireEvent.change(refInput, { target: { value: 'bad!' } });
    expect(payBtn.disabled).toBe(true);

    // Valid pattern unlocks Pay and submits reference_no
    fireEvent.change(refInput, { target: { value: 'MM-2026-7788' } });
    expect(payBtn.disabled).toBe(false);

    fireEvent.click(payBtn);
    await waitFor(() => {
      const makeCall = mockCallPost.mock.calls.find((c) => String(c[0]).includes('make_invoice'));
      expect(makeCall).toBeTruthy();
      expect(makeCall![1].payments).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            mode_of_payment: 'MTN MoMo',
            amount: 50000,
            reference_no: 'MM-2026-7788',
          }),
        ]),
      );
    });
  });

  it('G22: PaymentDialog shows USD FX tender badge, live UGX equivalent, local UGX change due, and sends custom_tender_amount', async () => {
    const { default: PaymentDialog } = await import('../src/components/PaymentDialog');

    render(
      <PaymentDialog
        onClose={vi.fn()}
        grandTotal={350000}
        roundedTotal={350000}
        invoice="POS-INV-FX-1"
        customer="Walk-in Guest"
        posProfile="Main Till"
        table={null}
        cashier="cashier@hotel.test"
        owner="cashier@hotel.test"
        fetchOrders={vi.fn().mockResolvedValue(undefined)}
        clearSelectedOrder={vi.fn()}
      />,
    );

    expect(screen.getByTestId('fx-badge-USD Cash')).toHaveTextContent(/USD/);

    const usdInput = screen.getByTestId('payment-fx-amount-USD Cash') as HTMLInputElement;
    expect(usdInput.step).toBe('0.01');
    fireEvent.change(usdInput, { target: { value: '100' } });

    expect(screen.getByTestId('fx-equiv-USD Cash')).toHaveTextContent(/370000|370,000/);
    expect(screen.getByTestId('fx-local-change')).toHaveTextContent(/20000|20,000/);

    const payBtn = screen.getByTestId('payment-submit-btn') as HTMLButtonElement;
    expect(payBtn.disabled).toBe(false);
    fireEvent.click(payBtn);

    await waitFor(() => {
      const makeCall = mockCallPost.mock.calls.find((c) => String(c[0]).includes('make_invoice'));
      expect(makeCall).toBeTruthy();
      expect(makeCall![1].payments).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            mode_of_payment: 'USD Cash',
            custom_tender_currency: 'USD',
            custom_tender_amount: 100,
          }),
        ]),
      );
    });
  });

  it('G22: PaymentDialog locks Pay with stale-rate alert when FX tender has stale/missing rate', async () => {
    const { default: PaymentDialog } = await import('../src/components/PaymentDialog');
    mockUsePOSStore.mockReturnValue({
      paymentModes: ['USD Cash'],
      paymentModeDetails: {
        'USD Cash': {
          mode_of_payment: 'USD Cash',
          type: 'Cash',
          custom_reference_mode: 'Hidden',
          custom_tender_currency: 'USD',
          custom_fx_rate_type: 'Buying',
          exchange_rate: null,
          fx_rate_status: 'stale',
        },
      },
      fetchPaymentModes: vi.fn(),
      currency: 'UGX',
      posProfile: {
        name: 'Main Till',
        currency: 'UGX',
        default_payment_mode: 'USD Cash',
        enable_payment_without_printing: 1,
        custom_allow_fx_rate_override: 0,
      },
    });

    render(
      <PaymentDialog
        onClose={vi.fn()}
        grandTotal={350000}
        roundedTotal={350000}
        invoice="POS-INV-FX-STALE"
        customer="Walk-in Guest"
        posProfile="Main Till"
        table={null}
        cashier="cashier@hotel.test"
        owner="cashier@hotel.test"
        fetchOrders={vi.fn().mockResolvedValue(undefined)}
        clearSelectedOrder={vi.fn()}
      />,
    );

    expect(screen.getByTestId('fx-stale-alert-USD Cash')).toBeInTheDocument();
    const payBtn = screen.getByTestId('payment-submit-btn') as HTMLButtonElement;
    expect(payBtn.disabled).toBe(true);
  });

  it('G25 + G22: SettledBills displays payment reference_no and foreign currency tender details', async () => {
    const { default: SettledBills } = await import('../src/pages/SettledBills');
    mockCallGet.mockResolvedValueOnce({
      message: [
        {
          name: 'POS-INV-777',
          customer: 'Walk-in Guest',
          status: 'Paid',
          posting_date: '2026-11-24',
          posting_time: '12:30:00',
          currency: 'UGX',
          restaurant_table: 'T-05',
          pos_profile: 'Main Till',
          consolidated_invoice: null,
          grand_total: 350000,
          rounded_total: 350000,
          outstanding_amount: 0,
          total_taxes_and_charges: 0,
          items: [{ item_name: 'Steak', qty: 1, amount: 350000 }],
          taxes: [],
          payments: [
            {
              mode_of_payment: 'MTN MoMo',
              amount: 100000,
              reference_no: 'MOMO-REF-555',
            },
            {
              mode_of_payment: 'USD Cash',
              amount: 250000,
              custom_tender_currency: 'USD',
              custom_tender_amount: 67.57,
              custom_exchange_rate: 3700,
            },
          ],
        },
      ],
    });

    render(
      <MemoryRouter>
        <SettledBills />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: /Search/i }));
    const billBtn = await screen.findByText('POS-INV-777');
    fireEvent.click(billBtn);

    expect(await screen.findByText(/MOMO-REF-555/)).toBeInTheDocument();
    expect(screen.getByText(/USD/)).toBeInTheDocument();
  });
});
