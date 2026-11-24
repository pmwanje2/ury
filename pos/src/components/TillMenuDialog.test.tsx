import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import Header from './Header';
import TillCashDialog from './TillCashDialog';

vi.mock('./PrinterWatch', () => ({
  PrinterStatusButton: () => null,
}));

const rootInit = useRootStore.getState();
const posInit = usePOSStore.getState();

beforeEach(async () => {
  await initI18n('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useRootStore.setState(rootInit, true);
  usePOSStore.setState(posInit, true);
});

const CASHIER_STATE = {
  enabled: true,
  pos_profile: 'Main Till',
  company: 'Maximus Hospitality Ltd',
  currency: 'UGX',
  opening_entry: 'POS-OPE-2026-00015',
  opening_user: 'cashier@example.com',
  paid_out_threshold: 50000,
  paid_in_threshold: 50000,
  cash_pull_threshold: 500000,
  can_approve: false,
  current_user: 'cashier@example.com',
  available_types: { Pickup: true, Loan: true, 'Paid Out': true, 'Paid In': true },
  paid_out_targets: [
    { kind: 'expense', label: 'Operating Paid-Out' },
    { kind: 'tips', label: 'Tip Payout' },
  ],
  movements: [],
  net_drawer_movement: 0,
  pending_count: 0,
};

const DRAWER_STATUS_OVER_THRESHOLD = {
  pos_profile: 'Main Till',
  currency: 'UGX',
  opening_entry: 'POS-OPE-2026-00015',
  opening_user: 'cashier@example.com',
  blind_close: false,
  cash_pull_threshold: 500000,
  threshold_exceeded: true,
  expected_drawer_cash: 620000,
  suggested_pickup_amount: 370000,
  threshold_exception_count: 0,
  no_sale_count: 2,
  over_short_tolerance: 5000,
  paid_out_threshold: 50000,
  paid_in_threshold: 50000,
};

function setupMockCalls(overrides?: {
  drawerStatus?: Record<string, unknown>;
  closedTills?: Array<Record<string, unknown>>;
  xReport?: Record<string, unknown>;
  reasons?: Array<{ code: string; description: string; requires_note: number; applies_to: string[] }>;
  loanRequiresAuth?: boolean;
}) {
  const getSpy = vi.spyOn(call, 'get').mockImplementation((method: string) => {
    if (method === 'ury.ury_pos.cash_movements.get_till_cash_state') {
      return Promise.resolve({ message: CASHIER_STATE });
    }
    if (method === 'ury.ury_pos.till_controls.get_till_drawer_status') {
      return Promise.resolve({ message: overrides?.drawerStatus ?? DRAWER_STATUS_OVER_THRESHOLD });
    }
    if (method === 'ury.ury_pos.override.list_approvers') {
      return Promise.resolve({ message: [{ user: 'manager@example.com', full_name: 'Alex Manager' }] });
    }
    if (method === 'ury.ury_pos.override.privilege_policy') {
      return Promise.resolve({ message: { may_perform: false, requires_reason: true } });
    }
    if (method === 'ury.ury_pos.override.list_reasons') {
      return Promise.resolve({ message: overrides?.reasons ?? [] });
    }
    if (method === 'ury.ury_pos.till_controls.list_closed_tills') {
      return Promise.resolve({
        message: overrides?.closedTills ?? [
          {
            name: 'POS-CLO-2026-0009',
            pos_opening_entry: 'POS-OPE-2026-00014',
            user: 'cashier@example.com',
            user_name: 'Grace Cashier',
            posting_date: '2026-11-24',
            period_end_date: '2026-11-24 14:00:00',
            status: 'Submitted',
            force_closed: false,
            no_sale_count: 1,
          },
        ],
      });
    }
    if (method === 'ury.ury_pos.till_controls.get_shift_x_report') {
      return Promise.resolve({
        message: overrides?.xReport ?? {
          pos_profile: 'Main Till',
          pos_opening_entry: 'POS-OPE-2026-00015',
          currency: 'UGX',
          blind_close: false,
          expected_drawer_cash: 620000,
          net_drawer_movement: -50000,
          no_sale_count: 3,
          no_sale_events: [
            { name: 'PDE-1', operator: 'cashier@example.com', approver: 'manager@example.com', reason: 'Make change', event_time: '2026-11-24 10:05:00' },
          ],
          threshold_exception_count: 1,
          threshold_exceptions: [],
          movements: [],
          open_checks_count: 2,
          open_checks: [],
        },
      });
    }
    return Promise.resolve({ message: {} });
  });

  const postSpy = vi.spyOn(call, 'post').mockImplementation((method: string) => {
    if (method === 'ury.ury_pos.override.authorise') {
      return Promise.resolve({
        message: { token: 'mgr-pin-token-123', approver: 'manager@example.com', approver_name: 'Alex Manager', expires_in: 120 },
      });
    }
    if (method === 'ury.ury_pos.till_controls.record_cash_movement') {
      return Promise.resolve({
        message: { name: 'ACC-JV-99', docstatus: 1, status: 'Submitted', approver: 'manager@example.com' },
      });
    }
    if (method === 'ury.ury_pos.cash_movements.record_till_cash_movement' && overrides?.loanRequiresAuth) {
      return Promise.reject({ message: '[POS-AUTH-REQUIRED] A manager must approve this action with their PIN.' });
    }
    if (method === 'ury.ury_pos.till_controls.trigger_no_sale') {
      return Promise.resolve({
        message: { event_name: 'PDE-2026-00001', drawer_kick_hex: '1b700019fa', no_sale_count_for_shift: 3, approver: 'manager@example.com' },
      });
    }
    if (method === 'ury.ury_pos.till_controls.log_cash_pull_exception') {
      return Promise.resolve({
        message: { name: 'PDE-2026-00002', event_type: 'Threshold Exception', approver: 'manager@example.com' },
      });
    }
    if (method === 'ury.ury_pos.till_controls.reopen_till') {
      return Promise.resolve({
        message: { pos_closing_entry: 'POS-CLO-2026-0009', status: 'Open', approver: 'manager@example.com' },
      });
    }
    return Promise.resolve({ message: {} });
  });

  return { getSpy, postSpy };
}

describe('Till Menu controls (Paid Out/In PIN, Safe Drop Threshold Exception, No Sale, Reopen, X-Report)', () => {
  it('opens in-place ManagerApproval PIN for Paid Out above limit with reason and reference (never redirects to Desk)', async () => {
    const { postSpy } = setupMockCalls();
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX', paid_in_enabled: true } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^Paid Out$/i })).toBeInTheDocument());

    // Paid In key is also present in the Till menu
    expect(screen.getByRole('button', { name: /^Paid In$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^No Sale$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Reopen Till/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /X-Report/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Paid Out$/i }));
    fireEvent.change(screen.getByLabelText(/Amount/i), { target: { value: '120000' } });
    fireEvent.change(screen.getByLabelText(/^Reason/i), { target: { value: 'Ice delivery' } });
    fireEvent.change(screen.getByLabelText(/Reference/i), { target: { value: 'RCP-881' } });
    fireEvent.click(screen.getByRole('button', { name: /Approve & Record Paid Out/i }));

    // ManagerApproval PIN modal opens in place
    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.record_cash_movement',
        expect.objectContaining({
          pos_profile: 'Main Till',
          movement_type: 'Paid Out',
          amount: 120000,
          reason: 'Ice delivery',
          reference: 'RCP-881',
          approval_token: 'mgr-pin-token-123',
        }),
      ),
    );
    // Cashier session NEVER changed
    expect(useRootStore.getState().user?.name).toBe('cashier@example.com');
  });

  it('shows Cash Pull threshold prompt (hiding expected cash in blind mode) and logs PIN-approved threshold exception', async () => {
    const { postSpy } = setupMockCalls({
      drawerStatus: {
        ...DRAWER_STATUS_OVER_THRESHOLD,
        blind_close: true,
        expected_drawer_cash: null,
        suggested_pickup_amount: null,
      },
    });
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX', custom_blind_cash_count: 1 } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/safe drop|cash pull|threshold/i);
    // Blind close: MUST NOT display 620,000 or 620000
    expect(document.body.textContent).not.toContain('620,000');
    expect(document.body.textContent).not.toContain('620000');

    fireEvent.click(screen.getByRole('button', { name: /Log Exception/i }));
    fireEvent.change(screen.getByLabelText(/Exception reason/i), { target: { value: 'Rush hour' } });
    fireEvent.click(screen.getByRole('button', { name: /Approve Exception/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.log_cash_pull_exception',
        expect.objectContaining({
          pos_profile: 'Main Till',
          reason: 'Rush hour',
          approval_token: 'mgr-pin-token-123',
        }),
      ),
    );
  });

  it('executes audited No Sale with reason and Manager PIN, and shows No Sale count on X-Report', async () => {
    const { postSpy } = setupMockCalls();
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX' } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^No Sale$/i })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^No Sale$/i }));
    fireEvent.change(screen.getByLabelText(/No Sale reason/i), { target: { value: 'Make change for guest' } });
    fireEvent.click(screen.getByRole('button', { name: /Open Cash Drawer/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.trigger_no_sale',
        expect.objectContaining({
          pos_profile: 'Main Till',
          reason: 'Make change for guest',
          approval_token: 'mgr-pin-token-123',
        }),
      ),
    );

    // Switch to X-Report tab and verify No Sale count is displayed
    fireEvent.click(screen.getByRole('button', { name: /X-Report/i }));
    expect(await screen.findByText(/No Sale/i)).toBeInTheDocument();
    expect(await screen.findByTestId('x-report-no-sale-count')).toHaveTextContent('3');
  });

  it('reopens a closed till with reason and Manager PIN', async () => {
    const { postSpy } = setupMockCalls();
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX' } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /Reopen Till/i })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Reopen Till/i }));
    expect(await screen.findByText(/POS-CLO-2026-0009/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Reopen reason/i), { target: { value: 'Late correction' } });
    fireEvent.click(screen.getByRole('button', { name: /Reopen Shift/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.reopen_till',
        expect.objectContaining({
          pos_closing_entry: 'POS-CLO-2026-0009',
          reason: 'Late correction',
          approval_token: 'mgr-pin-token-123',
        }),
      ),
    );
  });

  it('opens the Till Menu from Header with Paid In, No Sale, Reopen Till, and X-Report without vendor words', async () => {
    setupMockCalls();
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX', paid_in_enabled: true } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>,
    );
    const btn = screen.getByRole('button', { name: /Till Cash/i });
    expect(btn).toBeInTheDocument();
    fireEvent.click(btn);
    expect(await screen.findByRole('button', { name: /^Paid In$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^No Sale$/i })).toBeInTheDocument();
    const txt = document.body.textContent?.toLowerCase() ?? '';
    expect(txt).not.toContain('ury');
    expect(txt).not.toContain('kamra');
    expect(txt).not.toContain('huf');
  });

  it('prompts active POS Reason via ManagerApproval for cash_loan when policy requires_reason and forwards reason to record_cash_movement', async () => {
    const { postSpy } = setupMockCalls({
      loanRequiresAuth: true,
      reasons: [{ code: 'CASH-DROP', description: 'Safe drop / cash pickup', requires_note: 0, applies_to: ['cash_pickup', 'cash_loan'] }],
    });
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX' } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^Loan$/i })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^Loan$/i }));
    fireEvent.change(screen.getByLabelText(/Amount/i), { target: { value: '50000' } });
    fireEvent.click(screen.getByRole('button', { name: /Record Loan/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    const reasonSelect = await screen.findByLabelText(/Reason/i);
    fireEvent.change(reasonSelect, { target: { value: 'CASH-DROP' } });
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.override.authorise',
        expect.objectContaining({
          privilege: 'cash_loan',
          claim: expect.objectContaining({ reason: 'Safe drop / cash pickup' }),
        }),
      ),
    );
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.record_cash_movement',
        expect.objectContaining({
          movement_type: 'Loan',
          amount: 50000,
          reason: 'Safe drop / cash pickup',
          approval_token: 'mgr-pin-token-123',
        }),
      ),
    );
  });

  it('wraps free-text Paid Out reason under active catch-all POS Reason for #147 R2 validation', async () => {
    const { postSpy } = setupMockCalls({
      reasons: [
        { code: 'PAIDOUT-SUPPLIES', description: 'Petty cash supplies purchase', requires_note: 1, applies_to: ['paid_out'] },
        { code: 'OTHER', description: 'Other', requires_note: 1, applies_to: [] },
      ],
    });
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX', paid_in_enabled: true } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^Paid Out$/i })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^Paid Out$/i }));
    fireEvent.change(screen.getByLabelText(/Amount/i), { target: { value: '120000' } });
    fireEvent.change(screen.getByLabelText(/^Reason/i), { target: { value: 'Ice delivery' } });
    fireEvent.change(screen.getByLabelText(/Reference/i), { target: { value: 'RCP-881' } });
    fireEvent.click(screen.getByRole('button', { name: /Approve & Record Paid Out/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.override.authorise',
        expect.objectContaining({
          privilege: 'paid_out',
          claim: expect.objectContaining({ reason: 'Other: Ice delivery' }),
        }),
      ),
    );
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.record_cash_movement',
        expect.objectContaining({
          movement_type: 'Paid Out',
          reason: 'Other: Ice delivery',
          approval_token: 'mgr-pin-token-123',
        }),
      ),
    );
  });
});
