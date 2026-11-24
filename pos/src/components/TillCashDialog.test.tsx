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

const SAMPLE_STATE = {
  enabled: true,
  pos_profile: 'Main Till',
  company: 'Maximus Hospitality Ltd',
  currency: 'UGX',
  opening_entry: 'POS-OPE-2026-00015',
  opening_user: 'cashier@example.com',
  paid_out_threshold: 5000,
  can_approve: true,
  current_user: 'manager@example.com',
  available_types: { Pickup: true, Loan: true, 'Paid Out': true },
  paid_out_targets: [
    { kind: 'expense', label: 'Operating Paid-Out' },
    { kind: 'tips', label: 'Tip Payout' },
  ],
  movements: [
    {
      name: 'ACC-JV-1',
      movement_type: 'Pickup',
      amount: 20000,
      drawer_change: -20000,
      reason: '',
      target_kind: 'safe',
      status: 'Submitted',
      docstatus: 1,
      owner: 'cashier@example.com',
      owner_name: 'Shift Cashier',
      approver: '',
      approver_name: '',
      creation: '2026-11-24 10:00:00',
      can_approve: false,
      can_discard: false,
    },
    {
      name: 'ACC-JV-2',
      movement_type: 'Paid Out',
      amount: 8000,
      drawer_change: -8000,
      reason: 'Kitchen gas cylinder',
      target_kind: 'expense',
      status: 'Pending Approval',
      docstatus: 0,
      owner: 'cashier@example.com',
      owner_name: 'Shift Cashier',
      approver: '',
      approver_name: '',
      creation: '2026-11-24 10:15:00',
      can_approve: true,
      can_discard: true,
    },
  ],
  net_drawer_movement: -20000,
  pending_count: 1,
};

describe('Header Till Cash visibility', () => {
  it('shows the Till Cash button only when enabled on the POS Profile', () => {
    vi.spyOn(call, 'get').mockResolvedValue({ message: { has_permission: false } });
    useRootStore.setState({ user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['URY Cashier'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 0 } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    const { rerender } = render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>,
    );
    expect(screen.queryByRole('button', { name: /Till Cash/i })).toBeNull();

    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1 } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });
    rerender(
      <BrowserRouter>
        <Header />
      </BrowserRouter>,
    );
    expect(screen.getByRole('button', { name: /Till Cash/i })).toBeInTheDocument();
  });
});

describe('TillCashDialog cashier touchscreen movements', () => {
  it('records Pickup, Loan, and Paid Out (with threshold notice & target) and approves/discards pending items', async () => {
    const getSpy = vi.spyOn(call, 'get').mockResolvedValue({ message: SAMPLE_STATE });
    const postSpy = vi.spyOn(call, 'post').mockResolvedValue({
      message: { name: 'ACC-JV-3', status: 'Submitted', docstatus: 1 },
    });

    useRootStore.setState({ user: { name: 'manager@example.com', full_name: 'Floor Manager', roles: ['URY Manager'] } });
    usePOSStore.setState({
      posProfile: { name: 'Main Till', enable_cash_movements: 1, currency: 'UGX' } as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    render(<TillCashDialog open onOpenChange={vi.fn()} />);

    await waitFor(() =>
      expect(getSpy).toHaveBeenCalledWith('ury.ury_pos.cash_movements.get_till_cash_state', {
        pos_profile: 'Main Till',
      }),
    );

    // Simphony touchscreen movement keys visible, zero vendor branding.
    expect(screen.getByRole('button', { name: /^Pickup$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Loan$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Paid Out$/i })).toBeInTheDocument();
    const text = document.body.textContent?.toLowerCase() ?? '';
    expect(text).not.toContain('ury');
    expect(text).not.toContain('kamra');
    expect(text).not.toContain('huf');

    // Record Pickup.
    fireEvent.click(screen.getByRole('button', { name: /^Pickup$/i }));
    fireEvent.change(screen.getByLabelText(/Amount/i), { target: { value: '15000' } });
    fireEvent.click(screen.getByRole('button', { name: /Record Pickup/i }));
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('ury.ury_pos.cash_movements.record_till_cash_movement', {
        pos_profile: 'Main Till',
        movement_type: 'Pickup',
        amount: 15000,
      }),
    );

    // Record Loan.
    fireEvent.click(screen.getByRole('button', { name: /^Loan$/i }));
    fireEvent.change(screen.getByLabelText(/Amount/i), { target: { value: '10000' } });
    fireEvent.click(screen.getByRole('button', { name: /Record Loan/i }));
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('ury.ury_pos.cash_movements.record_till_cash_movement', {
        pos_profile: 'Main Till',
        movement_type: 'Loan',
        amount: 10000,
      }),
    );

    // Record Paid Out above threshold with Tip Payout target and reason.
    fireEvent.click(screen.getByRole('button', { name: /^Paid Out$/i }));
    fireEvent.change(screen.getByLabelText(/Amount/i), { target: { value: '8000' } });
    expect(screen.getByText(/second-person approval/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Tip Payout/i }));
    fireEvent.change(screen.getByLabelText(/Reason/i), { target: { value: 'Waiter tips' } });
    fireEvent.click(screen.getByRole('button', { name: /Record Paid Out/i }));
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('ury.ury_pos.cash_movements.record_till_cash_movement', {
        pos_profile: 'Main Till',
        movement_type: 'Paid Out',
        amount: 8000,
        reason: 'Waiter tips',
        paid_out_target: 'tips',
      }),
    );

    // Approve & Discard actions for pending movement.
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('ury.ury_pos.cash_movements.approve_till_cash_movement', {
        journal_entry: 'ACC-JV-2',
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: /^Discard$/i }));
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('ury.ury_pos.cash_movements.discard_till_cash_movement', {
        journal_entry: 'ACC-JV-2',
      }),
    );
  });
});
