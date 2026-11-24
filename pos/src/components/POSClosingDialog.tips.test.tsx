import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import POSClosingDialog from './POSClosingDialog';

const getOpenPosOpeningEntriesMock = vi.fn();
const getSubCashierPosInvoicesMock = vi.fn();
const getMainCashierPosInvoicesMock = vi.fn();
const createPosClosingEntryMock = vi.fn();
const submitPosClosingEntryMock = vi.fn();
const getChecklistMock = vi.fn();
const callGetMock = vi.fn();

const { mockPosProfile, mockUser, mockPaymentModes } = vi.hoisted(() => ({
  mockPosProfile: {
    name: 'Main Grill POS',
    company: 'Maximus Hospitality',
    currency: 'UGX',
    owner: 'cashier@example.com',
    multiple_cashier: 0,
    custom_blind_cash_count: 0,
    require_over_short_reason: 0,
    over_short_reason_tolerance: 0,
    require_tip_declaration: 0,
    payment_mode_meta: {
      Cash: { type: 'Cash', overtender_rule: 'Refuse' },
    } as Record<string, { type: string; overtender_rule: string }>,
  },
  mockUser: { name: 'cashier@example.com', full_name: 'Shift Cashier' },
  mockPaymentModes: ['Cash'],
}));

vi.mock('../lib/pos-closing-api', () => ({
  getOpenPosOpeningEntries: (...args: unknown[]) => getOpenPosOpeningEntriesMock(...args),
  getSubCashierPosInvoices: (...args: unknown[]) => getSubCashierPosInvoicesMock(...args),
  getMainCashierPosInvoices: (...args: unknown[]) => getMainCashierPosInvoicesMock(...args),
  createSubPosClosing: vi.fn(),
  submitSubPosClosing: vi.fn(),
  createPosClosingEntry: (...args: unknown[]) => createPosClosingEntryMock(...args),
  submitPosClosingEntry: (...args: unknown[]) => submitPosClosingEntryMock(...args),
}));

vi.mock('../lib/checklist-api', () => ({
  getChecklist: (...args: unknown[]) => getChecklistMock(...args),
}));

vi.mock('@ury/core', () => ({
  shouldMaskAmounts: vi.fn(() => false),
  call: {
    get: (...args: unknown[]) => callGetMock(...args),
  },
  db: {
    getDoc: vi.fn().mockResolvedValue({
      name: 'POS-OPE-2026-00015',
      balance_details: [{ mode_of_payment: 'Cash', opening_amount: 50000 }],
    }),
  },
  formatCurrency: (amount: number, currency?: string) => `${currency || 'UGX'} ${amount}`,
}));

vi.mock('../store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: mockPosProfile,
    currency: mockPosProfile.currency,
    paymentModes: mockPaymentModes,
  }),
}));

vi.mock('../store/root-store', () => ({
  useRootStore: () => ({
    user: mockUser,
  }),
}));

vi.mock('../i18n', () => ({
  t: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}:${JSON.stringify(params)}` : key,
}));

const VENDOR_RE = /\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala|BuildSuite)\b/i;

describe('POSClosingDialog Tips Declaration (G38)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPosProfile.currency = 'UGX';
    mockPosProfile.require_tip_declaration = 0;
    getOpenPosOpeningEntriesMock.mockResolvedValue([
      {
        name: 'POS-OPE-2026-00015',
        user: 'cashier@example.com',
        period_start_date: '2026-11-24 08:00:00',
        pos_profile: 'Main Grill POS',
      },
    ]);
    getMainCashierPosInvoicesMock.mockResolvedValue([
      {
        name: 'POS-INV-1',
        grand_total: 50000,
        net_total: 42373,
        total_qty: 2,
        change_amount: 0,
        account_for_change_amount: 'Cash - MH',
        taxes: [],
        payments: [{ mode_of_payment: 'Cash', amount: 50000, account: 'Cash - MH' }],
      },
    ]);
    getChecklistMock.mockResolvedValue({ logStatus: 'Complete' });
    createPosClosingEntryMock.mockResolvedValue({ name: 'POS-CLO-1' });
    submitPosClosingEntryMock.mockResolvedValue({ name: 'POS-CLO-1' });
    callGetMock.mockImplementation((method: string) => {
      if (method === 'ury.ury.api.ury_server_time.get_server_time') {
        return Promise.resolve({ message: '2026-11-24T18:00:00' });
      }
      if (method === 'ury.ury_pos.cash_movements.get_shift_cash_movement_totals') {
        return Promise.resolve({ message: { by_mode: {}, pending_count: 0 } });
      }
      if (method === 'ury.ury_pos.tips_declaration.get_shift_tip_declaration_rows') {
        return Promise.resolve({
          message: {
            pos_opening_entry: 'POS-OPE-2026-00015',
            pos_profile: 'Main Grill POS',
            currency: mockPosProfile.currency,
            currency_precision: mockPosProfile.currency === 'KWD' ? 3 : mockPosProfile.currency === 'USD' ? 2 : 0,
            require_tip_declaration: mockPosProfile.require_tip_declaration === 1,
            rows: [
              {
                user: 'waiter1@example.com',
                employee: 'HR-EMP-001',
                full_name: 'Alice Nakato',
                currency: mockPosProfile.currency,
                charged_tips_collected: mockPosProfile.currency === 'KWD' ? 4.25 : 30000,
                tips_paid_from_till: mockPosProfile.currency === 'KWD' ? 1.0 : 10000,
                net_tips_payable: mockPosProfile.currency === 'KWD' ? 3.25 : 20000,
                declared_cash_tips: 0,
                remarks: '',
              },
            ],
          },
        });
      }
      return Promise.resolve({ message: {} });
    });
  });

  it('declares cash tips without distorting expected drawer cash or Over/Short variance', async () => {
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    // Expected drawer cash = 50,000 opening + 50,000 sales = 100,000
    expect(await screen.findByText('UGX 100000')).toBeInTheDocument();
    expect(await screen.findByText('Alice Nakato')).toBeInTheDocument();
    expect(document.body.textContent || '').not.toMatch(VENDOR_RE);

    // Count exact drawer cash 100,000
    const [closingInput] = screen.getAllByRole('spinbutton');
    fireEvent.change(closingInput, { target: { value: '100000' } });

    // Declare UGX 45,000 direct cash tips for Alice
    const tipInput = screen.getByLabelText(/pos_closing\.declared_cash_tips_for.*Alice Nakato/i);
    expect(tipInput).toHaveAttribute('step', '1');
    fireEvent.change(tipInput, { target: { value: '45000' } });

    // Expected cash stays 100,000 and variance stays 0 (NOT 145,000 or -45,000!)
    expect(screen.getByText('UGX 100000')).toBeInTheDocument();
    expect(screen.getByText('UGX 0')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'pos_closing.submit' }));
    fireEvent.click(await screen.findByRole('button', { name: 'pos_closing.confirm_submit' }));

    await waitFor(() => expect(createPosClosingEntryMock).toHaveBeenCalled());
    const payload = createPosClosingEntryMock.mock.calls[0][0];
    expect(payload.payment_reconciliation[0].expected_amount).toBe(100000);
    expect(payload.payment_reconciliation[0].difference).toBe(0);
    expect(payload.custom_tip_declarations).toEqual([
      expect.objectContaining({
        user: 'waiter1@example.com',
        employee: 'HR-EMP-001',
        declared_cash_tips: 45000,
      }),
    ]);
  });

  it('enforces required tip declaration and respects 3dp currency step (KWD)', async () => {
    mockPosProfile.currency = 'KWD';
    mockPosProfile.require_tip_declaration = 1;

    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    const tipInput = await screen.findByLabelText(/pos_closing\.declared_cash_tips_for.*Alice Nakato/i);
    expect(tipInput).toHaveAttribute('step', '0.001');

    // Submit blocked until required tip declaration is touched/entered
    expect(screen.getByRole('button', { name: 'pos_closing.submit' })).toBeDisabled();
    expect(screen.getByText(/pos_closing\.tip_declaration_required/i)).toBeInTheDocument();

    fireEvent.change(tipInput, { target: { value: '12.375' } });
    expect(screen.getByRole('button', { name: 'pos_closing.submit' })).not.toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'pos_closing.submit' }));
    fireEvent.click(await screen.findByRole('button', { name: 'pos_closing.confirm_submit' }));

    await waitFor(() => expect(createPosClosingEntryMock).toHaveBeenCalled());
    const payload = createPosClosingEntryMock.mock.calls[0][0];
    expect(payload.custom_tip_declarations[0].declared_cash_tips).toBe(12.375);
  });
});
