import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { call, db } from '@ury/core';
import { initI18n } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import POSClosingDialog from './POSClosingDialog';

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

function setupClosingMocks(opts?: {
  blind?: boolean;
  tolerance?: number;
  openEntries?: Array<{ name: string; user: string; user_name?: string; period_start_date: string; pos_profile: string; is_own?: boolean }>;
  closeThrowsBlindVarianceFirst?: boolean;
  reasons?: Array<{ code: string; description: string; requires_note: number; applies_to: string[] }>;
}) {
  const blind = opts?.blind ?? false;
  const tolerance = opts?.tolerance ?? 5000;
  const entries = opts?.openEntries ?? [
    {
      name: 'POS-OPE-1',
      user: 'cashier@example.com',
      user_name: 'Grace Cashier',
      period_start_date: '2026-11-24 08:00:00',
      pos_profile: 'Main Till',
      is_own: true,
    },
  ];

  useRootStore.setState({
    user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] },
  });
  usePOSStore.setState({
    paymentModes: ['Cash'],
    posProfile: {
      name: 'Main Till',
      company: 'Hotel Co',
      currency: 'UGX',
      owner: 'cashier@example.com',
      multiple_cashier: 0,
      custom_blind_cash_count: blind ? 1 : 0,
      over_short_tolerance: tolerance,
      payment_mode_meta: { Cash: { type: 'Cash', overtender_rule: 'Refuse', halo_max_amount: 500000 } },
    } as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
  });

  vi.spyOn(db, 'getDoc').mockResolvedValue({
    name: entries[0].name,
    balance_details: [{ mode_of_payment: 'Cash', opening_amount: 100000 }],
  } as never);

  const getSpy = vi.spyOn(call, 'get').mockImplementation((method: string) => {
    if (method === 'ury.ury_pos.api.get_open_pos_opening_entries') {
      return Promise.resolve({ message: entries });
    }
    if (method === 'ury.ury_pos.till_controls.list_open_tills') {
      return Promise.resolve({ message: entries });
    }
    if (method === 'ury.ury.api.ury_server_time.get_server_time') {
      return Promise.resolve({ message: '2026-11-24T18:00:00' });
    }
    if (method === 'ury.ury_pos.api.get_till_invoices') {
      return Promise.resolve({
        message: [
          {
            name: 'POS-INV-1',
            grand_total: 50000,
            net_total: 50000,
            total_qty: 1,
            change_amount: 0,
            account_for_change_amount: 'Cash - HC',
            taxes: [],
            payments: [{ mode_of_payment: 'Cash', amount: 50000, account: 'Cash - HC' }],
          },
        ],
      });
    }
    if (method === 'ury.ury_pos.cash_movements.get_shift_cash_movement_totals') {
      return Promise.resolve({ message: { by_mode: {}, pending_count: 0 } });
    }
    if (method === 'ury.ury_pos.till_controls.get_till_close_preview') {
      return Promise.resolve({
        message: {
          pos_profile: 'Main Till',
          pos_opening_entry: entries[0].name,
          opening_user: entries[0].user,
          blind_close: blind,
          over_short_tolerance: tolerance,
          no_sale_count: 2,
          open_checks_count: 1,
          open_checks: [{ name: 'POS-DRAFT-1', restaurant_table: 'T-4', grand_total: 25000 }],
          pending_movements_count: 0,
          pending_movements: [],
        },
      });
    }
    if (method === 'ury.ury_pos.api.get_checklist') {
      return Promise.resolve({ message: { log_status: 'Complete', items: [] } });
    }
    if (method === 'ury.ury_pos.override.list_approvers') {
      return Promise.resolve({ message: [{ user: 'manager@example.com', full_name: 'Alex Manager' }] });
    }
    if (method === 'ury.ury_pos.override.privilege_policy') {
      return Promise.resolve({ message: { may_perform: false, requires_reason: true } });
    }
    if (method === 'ury.ury_pos.override.list_reasons') {
      return Promise.resolve({ message: opts?.reasons ?? [] });
    }
    return Promise.resolve({ message: {} });
  });

  let closeCalls = 0;
  const postSpy = vi.spyOn(call, 'post').mockImplementation((method: string) => {
    if (method === 'ury.ury_pos.override.authorise') {
      return Promise.resolve({
        message: { token: 'close-pin-tok-1', approver: 'manager@example.com', approver_name: 'Alex Manager', expires_in: 120 },
      });
    }
    if (method === 'ury.ury_pos.till_controls.close_till_with_controls') {
      closeCalls += 1;
      if (opts?.closeThrowsBlindVarianceFirst && closeCalls === 1) {
        return Promise.reject({
          message: '[POS-CLOSE-VARIANCE-APPROVAL] Counted cash is outside this till\'s allowed variance. Recount the drawer or ask a manager to approve with their PIN and a reason.',
        });
      }
      return Promise.resolve({ message: { name: 'POS-CLO-100', docstatus: 1 } });
    }
    if (method === 'ury.ury_pos.till_controls.force_close_till') {
      return Promise.resolve({ message: { name: 'POS-CLO-101', docstatus: 1, force_closed: true } });
    }
    return Promise.resolve({ message: {} });
  });

  return { getSpy, postSpy };
}

describe('POSClosingDialog till controls (Over/Short PIN, Blind Close, Close Other, Force Close)', () => {
  it('requires Over/Short reason and ManagerApproval PIN when variance exceeds tolerance (including tolerance 0)', async () => {
    const { postSpy } = setupClosingMocks({ tolerance: 0 });
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    const input = await screen.findByRole('spinbutton');
    // Expected = 150,000; counted = 149,999 (diff = -1 > tolerance 0)
    fireEvent.change(input, { target: { value: '149999' } });

    expect(screen.getByText(/exceeds.*tolerance/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Over\/Short reason/i), { target: { value: 'Short 1 coin' } });
    fireEvent.click(screen.getByRole('button', { name: /^Close POS$/i }));

    // Opens ManagerApproval for over_short_close
    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.close_till_with_controls',
        expect.objectContaining({
          pos_profile: 'Main Till',
          pos_opening_entry: 'POS-OPE-1',
          over_short_reason: 'Short 1 coin',
          over_short_approval_token: 'close-pin-tok-1',
        }),
      ),
    );
  });

  it('keeps blind close expected cash hidden and offers Recount or Manager PIN on server variance refusal', async () => {
    const { postSpy } = setupClosingMocks({ blind: true, tolerance: 5000, closeThrowsBlindVarianceFirst: true });
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    const input = await screen.findByRole('spinbutton');
    // Blind close MUST NOT show expected 150,000 anywhere in DOM
    expect(document.body.textContent).not.toContain('150,000');
    expect(document.body.textContent).not.toContain('150000');

    fireEvent.change(input, { target: { value: '120000' } });
    fireEvent.click(screen.getByRole('button', { name: /^Close POS$/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Yes, Close POS/i }));

    // Server returns blind POS-CLOSE-VARIANCE-APPROVAL -> shows Recount & Manager Approval options without leaking expected
    expect(await screen.findByText(/outside this till's allowed variance/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('150,000');
    expect(document.body.textContent).not.toContain('150000');

    fireEvent.change(screen.getByLabelText(/Over\/Short reason/i), { target: { value: 'Blind recount confirmed short' } });
    fireEvent.click(screen.getByRole('button', { name: /Approve Variance with PIN/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.close_till_with_controls',
        expect.objectContaining({
          over_short_reason: 'Blind recount confirmed short',
          over_short_approval_token: 'close-pin-tok-1',
        }),
      ),
    );
  });

  it('supports closing another person\'s till with ManagerApproval PIN and active POS Reason', async () => {
    const { postSpy } = setupClosingMocks({
      openEntries: [
        {
          name: 'POS-OPE-OTHER',
          user: 'other.cashier@example.com',
          user_name: 'Peter Cashier',
          period_start_date: '2026-11-24 08:00:00',
          pos_profile: 'Main Till',
          is_own: false,
        },
      ],
      reasons: [{ code: 'OTHER', description: 'Other', requires_note: 1, applies_to: [] }],
    });

    render(<POSClosingDialog open onOpenChange={vi.fn()} />);
    // Selects the other cashier's till
    expect(await screen.findByText(/Peter Cashier/i)).toBeInTheDocument();
    const input = await screen.findByRole('spinbutton');
    fireEvent.change(input, { target: { value: '150000' } });
    fireEvent.click(screen.getByRole('button', { name: /^Close POS$/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    const reasonSelect = await screen.findByLabelText(/Reason/i);
    fireEvent.change(reasonSelect, { target: { value: 'OTHER' } });
    const noteBox = await screen.findByRole('textbox');
    fireEvent.change(noteBox, { target: { value: 'Cashier handed over till' } });
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.close_till_with_controls',
        expect.objectContaining({
          pos_opening_entry: 'POS-OPE-OTHER',
          close_other_approval_token: 'close-pin-tok-1',
          close_other_reason: 'Other: Cashier handed over till',
        }),
      ),
    );
  });

  it('formats free-text Over/Short reason under active catch-all POS Reason for #147 R2 validation', async () => {
    const { postSpy } = setupClosingMocks({
      tolerance: 0,
      reasons: [{ code: 'OTHER', description: 'Other', requires_note: 1, applies_to: [] }],
    });
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    const input = await screen.findByRole('spinbutton');
    fireEvent.change(input, { target: { value: '145000' } });
    fireEvent.change(screen.getByLabelText(/Over\/Short reason/i), { target: { value: 'Short 5000 coin roll' } });
    fireEvent.click(screen.getByRole('button', { name: /^Close POS$/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.override.authorise',
        expect.objectContaining({
          privilege: 'over_short_close',
          claim: expect.objectContaining({ reason: 'Other: Short 5000 coin roll' }),
        }),
      ),
    );
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.close_till_with_controls',
        expect.objectContaining({
          over_short_reason: 'Other: Short 5000 coin roll',
          over_short_approval_token: 'close-pin-tok-1',
        }),
      ),
    );
  });

  it('supports Force Close with open checks warning, mandatory reason, and ManagerApproval PIN', async () => {
    const { postSpy } = setupClosingMocks();
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    const input = await screen.findByRole('spinbutton');
    fireEvent.change(input, { target: { value: '150000' } });

    fireEvent.click(screen.getByRole('button', { name: /Force Close/i }));
    expect(await screen.findByText(/1 open check/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Force close reason/i), { target: { value: 'Night audit cutoff' } });
    fireEvent.click(screen.getByRole('button', { name: /Approve Force Close/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.force_close_till',
        expect.objectContaining({
          pos_profile: 'Main Till',
          pos_opening_entry: 'POS-OPE-1',
          reason: 'Night audit cutoff',
          approval_token: 'close-pin-tok-1',
        }),
      ),
    );
  });

  it('preserves close_other_approval_token across blind variance refusal so second step sends both tokens', async () => {
    const { postSpy } = setupClosingMocks({
      blind: true,
      tolerance: 5000,
      closeThrowsBlindVarianceFirst: true,
      openEntries: [
        {
          name: 'POS-OPE-OTHER',
          user: 'other.cashier@example.com',
          user_name: 'Peter Cashier',
          period_start_date: '2026-11-24 08:00:00',
          pos_profile: 'Main Till',
          is_own: false,
        },
      ],
      reasons: [{ code: 'OTHER', description: 'Other', requires_note: 1, applies_to: [] }],
    });
    let authCalls = 0;
    const origImpl = postSpy.getMockImplementation()!;
    postSpy.mockImplementation((method: string, body?: unknown) => {
      if (method === 'ury.ury_pos.override.authorise') {
        authCalls += 1;
        return Promise.resolve({
          message: {
            token: authCalls === 1 ? 'close-other-tok-1' : 'over-short-tok-2',
            approver: 'manager@example.com',
            approver_name: 'Alex Manager',
            expires_in: 120,
          },
        });
      }
      return origImpl(method, body as never);
    });

    render(<POSClosingDialog open onOpenChange={vi.fn()} />);
    expect(await screen.findByText(/Peter Cashier/i)).toBeInTheDocument();

    const input = await screen.findByRole('spinbutton');
    fireEvent.change(input, { target: { value: '120000' } });
    fireEvent.click(screen.getByRole('button', { name: /^Close POS$/i }));

    // Step 1: approve close_other_till PIN -> backend refuses blind POS-CLOSE-VARIANCE-APPROVAL
    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    const reasonSelect = await screen.findByLabelText(/Reason/i);
    fireEvent.change(reasonSelect, { target: { value: 'OTHER' } });
    const noteBox = await screen.findByRole('textbox');
    fireEvent.change(noteBox, { target: { value: 'Cashier handed over till' } });
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    expect(await screen.findByText(/outside this till's allowed variance/i)).toBeInTheDocument();

    // Step 2: enter variance reason and approve over_short_close PIN
    fireEvent.change(screen.getByLabelText(/Over\/Short reason/i), {
      target: { value: 'Confirmed blind short on other till' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Approve Variance with PIN/i }));

    expect(await screen.findByText(/Manager authorisation required/i)).toBeInTheDocument();
    for (const digit of ['2', '4', '6', '8']) {
      fireEvent.click(await screen.findByRole('button', { name: digit }));
    }
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/i }));

    // Second close_till_with_controls call MUST carry BOTH preserved close_other_approval_token and over_short_approval_token
    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.close_till_with_controls',
        expect.objectContaining({
          pos_opening_entry: 'POS-OPE-OTHER',
          close_other_approval_token: 'close-other-tok-1',
          close_other_reason: 'Other: Cashier handed over till',
          over_short_approval_token: 'over-short-tok-2',
          over_short_reason: 'Other: Confirmed blind short on other till',
        }),
      ),
    );
  });
});
