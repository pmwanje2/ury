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
    name: 'Main Till',
    company: 'Maximus Hospitality Ltd',
    owner: 'cashier@example.com',
    multiple_cashier: 0,
    custom_blind_cash_count: 0,
  },
  mockUser: { name: 'cashier@example.com', full_name: 'Shift Cashier' },
  mockPaymentModes: ['Maximus Cash'],
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
      balance_details: [{ mode_of_payment: 'Maximus Cash', opening_amount: 50000 }],
    }),
  },
  formatCurrency: (amount: number) => `Sh ${amount}`,
}));

vi.mock('../store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: mockPosProfile,
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

describe('POSClosingDialog cash movements expected & difference', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPosProfile.custom_blind_cash_count = 0;
    getOpenPosOpeningEntriesMock.mockResolvedValue([
      {
        name: 'POS-OPE-2026-00015',
        user: 'cashier@example.com',
        period_start_date: '2026-11-24 08:00:00',
        pos_profile: 'Main Till',
      },
    ]);
    getMainCashierPosInvoicesMock.mockResolvedValue([
      {
        name: 'POS-INV-1',
        grand_total: 13800,
        net_total: 10000,
        total_qty: 1,
        change_amount: 0,
        account_for_change_amount: 'Till Cash - MH',
        taxes: [],
        payments: [{ mode_of_payment: 'Maximus Cash', amount: 13800, account: 'Till Cash - MH' }],
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
        return Promise.resolve({
          message: { by_mode: { 'Maximus Cash': -21000 }, pending_count: 0 },
        });
      }
      return Promise.resolve({ message: {} });
    });
  });

  it('includes submitted shift cash movements in expected cash and live difference', async () => {
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    // 50,000 opening + 13,800 sales - 21,000 net cash movements = 42,800 expected (not 63,800).
    expect(await screen.findByText('Sh 42800')).toBeInTheDocument();
    expect(screen.queryByText('Sh 63800')).toBeNull();

    const closingInput = screen.getByRole('spinbutton');
    fireEvent.change(closingInput, { target: { value: '40000' } });

    // 40,000 counted - 42,800 expected = -2,800 shortage (not -23,800).
    expect(screen.getByText('Sh -2800')).toBeInTheDocument();
    expect(screen.queryByText('Sh -23800')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'pos_closing.submit' }));
    fireEvent.click(await screen.findByRole('button', { name: 'pos_closing.confirm_submit' }));

    await waitFor(() => expect(createPosClosingEntryMock).toHaveBeenCalled());
    const payload = createPosClosingEntryMock.mock.calls[0][0];
    expect(payload.payment_reconciliation).toEqual([
      {
        mode_of_payment: 'Maximus Cash',
        opening_amount: 50000,
        expected_amount: 42800,
        closing_amount: 40000,
        difference: -2800,
      },
    ]);
  });

  it('warns when pending cash movements await approval before closing', async () => {
    callGetMock.mockImplementation((method: string) => {
      if (method === 'ury.ury.api.ury_server_time.get_server_time') {
        return Promise.resolve({ message: '2026-11-24T18:00:00' });
      }
      if (method === 'ury.ury_pos.cash_movements.get_shift_cash_movement_totals') {
        return Promise.resolve({
          message: { by_mode: { 'Maximus Cash': -3000 }, pending_count: 2 },
        });
      }
      return Promise.resolve({ message: {} });
    });

    render(<POSClosingDialog open onOpenChange={vi.fn()} />);
    expect(await screen.findByText(/pos_closing\.pending_cash_movements/)).toBeInTheDocument();
  });
});
