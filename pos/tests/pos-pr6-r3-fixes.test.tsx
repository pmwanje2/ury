import fs from 'node:fs';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { call, db } from '@ury/core';
import { initI18n } from '../src/i18n';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import POSClosingDialog from '../src/components/POSClosingDialog';
import ClosingPaymentTable from '../src/components/ClosingPaymentTable';
import PaymentDialog from '../src/components/PaymentDialog';
import { useCaptainContext } from '../src/captain/hooks/useCaptainContext';

const rootInit = useRootStore.getState();
const posInit = usePOSStore.getState();

describe('POS-PR-6 R3 RED -> GREEN: Controlled Close FX, UI layout, ReDoS guard, and Captain offline isolation', () => {
  const origOnline = Object.getOwnPropertyDescriptor(navigator, 'onLine');

  beforeEach(async () => {
    await initI18n('en');
    localStorage.clear();
    sessionStorage.clear();
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useRootStore.setState(rootInit, true);
    usePOSStore.setState(posInit, true);
    if (origOnline) Object.defineProperty(navigator, 'onLine', origOnline);
    else Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  });

  function setupFxControlledCloseMocks() {
    useRootStore.setState({
      user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] },
    });
    usePOSStore.setState({
      currency: 'UGX',
      paymentModes: ['Cash', 'USD Cash', 'KWD Cash'],
      paymentModeDetails: {
        Cash: {
          mode_of_payment: 'Cash',
          type: 'Cash',
          custom_reference_mode: 'Hidden',
          custom_tender_currency: '',
        },
        'USD Cash': {
          mode_of_payment: 'USD Cash',
          type: 'Cash',
          custom_reference_mode: 'Hidden',
          custom_tender_currency: 'USD',
          custom_fx_rate_type: 'Buying',
          exchange_rate: 3715,
          fx_rate_status: 'fresh',
        },
        'KWD Cash': {
          mode_of_payment: 'KWD Cash',
          type: 'Cash',
          custom_reference_mode: 'Hidden',
          custom_tender_currency: 'KWD',
          custom_fx_rate_type: 'Buying',
          exchange_rate: 12345,
          fx_rate_status: 'fresh',
        },
      } as any,
      posProfile: {
        name: 'Main Till',
        company: 'Hotel Co',
        currency: 'UGX',
        owner: 'cashier@example.com',
        multiple_cashier: 0,
        custom_blind_cash_count: 0,
        over_short_tolerance: 0,
        require_over_short_reason: 1,
        over_short_reason_tolerance: 0,
        payment_mode_meta: {
          Cash: { type: 'Cash', overtender_rule: 'Refuse', halo_max_amount: 500000 },
          'USD Cash': { type: 'Cash', overtender_rule: 'Refuse', halo_max_amount: 500000 },
          'KWD Cash': { type: 'Cash', overtender_rule: 'Refuse', halo_max_amount: 500000 },
        },
      } as any,
    });

    vi.spyOn(db, 'getDoc').mockResolvedValue({
      name: 'POS-OPE-FX-1',
      balance_details: [
        { mode_of_payment: 'Cash', opening_amount: 0 },
        { mode_of_payment: 'USD Cash', opening_amount: 0 },
        { mode_of_payment: 'KWD Cash', opening_amount: 0 },
      ],
    } as never);

    vi.spyOn(call, 'get').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.till_controls.list_open_tills' || method === 'ury.ury_pos.api.get_open_pos_opening_entries') {
        return Promise.resolve({
          message: [
            {
              name: 'POS-OPE-FX-1',
              user: 'cashier@example.com',
              user_name: 'Grace Cashier',
              period_start_date: '2026-11-24 08:00:00',
              pos_profile: 'Main Till',
              is_own: true,
            },
          ],
        });
      }
      if (method === 'ury.ury.api.ury_server_time.get_server_time') {
        return Promise.resolve({ message: '2026-11-24T18:00:00' });
      }
      if (method === 'ury.ury_pos.api.get_till_invoices') {
        return Promise.resolve({
          message: [
            {
              name: 'KR2B00049',
              grand_total: 62000,
              net_total: 62000,
              total_qty: 3,
              change_amount: 0,
              account_for_change_amount: 'Cash - HC',
              taxes: [],
              payments: [
                { mode_of_payment: 'Cash', amount: 36075, account: 'Cash - HC' },
                {
                  mode_of_payment: 'USD Cash',
                  amount: 12037,
                  account: 'FX Clearing USD - HC',
                  custom_tender_currency: 'USD',
                  custom_tender_amount: 3.24,
                  custom_exchange_rate: 3715.12345679,
                },
                {
                  mode_of_payment: 'KWD Cash',
                  amount: 13888,
                  account: 'FX Clearing KWD - HC',
                  custom_tender_currency: 'KWD',
                  custom_tender_amount: 1.125,
                  custom_exchange_rate: 12344.888888889,
                },
              ],
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
            pos_opening_entry: 'POS-OPE-FX-1',
            opening_user: 'cashier@example.com',
            blind_close: false,
            over_short_tolerance: 0,
            no_sale_count: 0,
            open_checks_count: 0,
            open_checks: [],
            pending_movements_count: 0,
            pending_movements: [],
          },
        });
      }
      if (method === 'ury.ury_pos.api.get_checklist') {
        return Promise.resolve({ message: { log_status: 'Complete', items: [] } });
      }
      return Promise.resolve({ message: {} });
    });

    const postSpy = vi.spyOn(call, 'post').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.till_controls.close_till_with_controls') {
        return Promise.resolve({ message: { name: 'POS-CLO-FX-1', docstatus: 1 } });
      }
      return Promise.resolve({ message: {} });
    });

    return { postSpy };
  }

  it('[P1 #1 UI] POSClosingDialog forwards custom_closing_foreign_amount to close_till_with_controls for FX modes', async () => {
    const { postSpy } = setupFxControlledCloseMocks();
    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    const usdInput = (await screen.findByTestId('closing-fx-input-USD Cash')) as HTMLInputElement;
    const kwdInput = screen.getByTestId('closing-fx-input-KWD Cash') as HTMLInputElement;
    const allSpinButtons = screen.getAllByRole('spinbutton') as HTMLInputElement[];
    const cashInput = allSpinButtons.find((el) => el !== usdInput && el !== kwdInput)!;

    fireEvent.change(cashInput, { target: { value: '36075' } });
    fireEvent.change(usdInput, { target: { value: '3.24' } });
    fireEvent.change(kwdInput, { target: { value: '1.125' } });

    const closeBtn = screen.getByRole('button', { name: /^Close POS$/i }) as HTMLButtonElement;
    expect(closeBtn.disabled).toBe(false);
    fireEvent.click(closeBtn);

    const confirmBtn = await screen.findByRole('button', { name: /Yes, Close POS/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      const closeCall = postSpy.mock.calls.find(
        (entry) => entry[0] === 'ury.ury_pos.till_controls.close_till_with_controls',
      );
      expect(closeCall).toBeTruthy();
      const body = closeCall![1] as {
        counted_payments: Array<{
          mode_of_payment: string;
          closing_amount: number;
          custom_closing_foreign_amount?: number;
        }>;
      };
      const byMode = Object.fromEntries(body.counted_payments.map((r) => [r.mode_of_payment, r]));
      expect(byMode['Cash'].closing_amount).toBe(36075);
      expect(byMode['Cash']).not.toHaveProperty('custom_closing_foreign_amount');
      expect(byMode['USD Cash'].custom_closing_foreign_amount).toBe(3.24);
      expect(byMode['KWD Cash'].custom_closing_foreign_amount).toBe(1.125);
    });
  });

  it('[P1 #1 UI] ClosingPaymentTable renders FX currency symbol on Opening column for FX modes', () => {
    const { container } = render(
      <ClosingPaymentTable
        currency="UGX"
        touchedModes={new Set()}
        onChange={vi.fn()}
        onForeignChange={vi.fn()}
        rows={[
          {
            mode_of_payment: 'Cash',
            opening_amount: 100000,
            expected_amount: 100000,
            closing_amount: 0,
            difference: -100000,
          },
          {
            mode_of_payment: 'USD Cash',
            opening_amount: 0,
            expected_amount: 14800,
            closing_amount: 0,
            difference: -14800,
            custom_tender_currency: 'USD',
            custom_expected_foreign_amount: 4,
            custom_closing_foreign_amount: 0,
            custom_foreign_difference: -4,
            effective_exchange_rate: 3700,
          } as any,
          {
            mode_of_payment: 'KWD Cash',
            opening_amount: 0,
            expected_amount: 13500,
            closing_amount: 0,
            difference: -13500,
            custom_tender_currency: 'KWD',
            custom_expected_foreign_amount: 1.125,
            custom_closing_foreign_amount: 0,
            custom_foreign_difference: -1.125,
            effective_exchange_rate: 12000,
          } as any,
        ]}
      />,
    );

    const rows = Array.from(container.querySelectorAll('tbody tr'));
    expect(rows).toHaveLength(3);
    const cashOpening = rows[0].querySelectorAll('td')[1].textContent || '';
    const usdOpening = rows[1].querySelectorAll('td')[1].textContent || '';
    const kwdOpening = rows[2].querySelectorAll('td')[1].textContent || '';

    expect(cashOpening).toMatch(/UGX/i);
    expect(usdOpening).toMatch(/USD|\$/i);
    expect(usdOpening).not.toMatch(/UGX/i);
    expect(kwdOpening).toMatch(/KWD/i);
    expect(kwdOpening).not.toMatch(/UGX/i);
  });

  it('[P1 #1 UI] POSClosingDialog keeps modal footer pinned and unclipped with scrollable body when Over/Short banner expands', async () => {
    setupFxControlledCloseMocks();
    const { container } = render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    await screen.findByTestId('closing-fx-input-USD Cash');
    const dialogRoot = container.querySelector('[role="dialog"]');
    expect(dialogRoot).toBeTruthy();

    const dialogCard = dialogRoot!.querySelector('.max-h-\\[90vh\\]') as HTMLElement | null;
    expect(dialogCard).toBeTruthy();
    expect(dialogCard!.className).toContain('flex-col');

    const scrollableBody = dialogCard!.querySelector('.overflow-y-auto.min-h-0') as HTMLElement | null;
    expect(scrollableBody).toBeTruthy();

    const closeBtn = screen.getByRole('button', { name: /^Close POS$/i });
    const footer = closeBtn.parentElement as HTMLElement;
    expect(footer.className).toContain('shrink-0');
  });

  it('[Minor #1] PaymentDialog has no duplicate ManagerApproval import', () => {
    const src = fs.readFileSync(
      path.resolve(__dirname, '../src/components/PaymentDialog.tsx'),
      'utf8',
    );
    const matches = src.match(/import\s+ManagerApproval\b/g) ?? [];
    expect(matches).toHaveLength(1);
  });

  it('[Minor #2] PaymentDialog client ReDoS guard rejects nested alternation quantifiers like ^(a|(b|c))+$', async () => {
    useRootStore.setState({
      user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] },
    });
    usePOSStore.setState({
      currency: 'UGX',
      paymentModes: ['MTN MoMo', 'Safe Card'],
      paymentModeDetails: {
        'MTN MoMo': {
          mode_of_payment: 'MTN MoMo',
          type: 'Bank',
          custom_reference_mode: 'Required',
          custom_reference_label: 'MoMo Ref',
          custom_reference_pattern: '^(a|(b|c))+$',
          custom_reference_date_required: 0,
          custom_tender_currency: '',
        },
        'Safe Card': {
          mode_of_payment: 'Safe Card',
          type: 'Bank',
          custom_reference_mode: 'Required',
          custom_reference_label: 'Card Ref',
          custom_reference_pattern: '^(?:MM|AP)-[0-9]{6}$',
          custom_reference_date_required: 0,
          custom_tender_currency: '',
        },
      } as any,
      posProfile: {
        name: 'Main Till',
        company: 'Hotel Co',
        currency: 'UGX',
        default_payment_mode: 'MTN MoMo',
        enable_payment_without_printing: 1,
      } as any,
    });
    vi.spyOn(call, 'get').mockResolvedValue({ message: {} });

    render(
      <PaymentDialog
        onClose={vi.fn()}
        grandTotal={50000}
        roundedTotal={50000}
        invoice="POS-INV-REDOS"
        customer="Walk-in Guest"
        posProfile="Main Till"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn().mockResolvedValue(undefined)}
        clearSelectedOrder={vi.fn()}
      />,
    );

    const momoAmt = screen.getByTestId('payment-amount-MTN MoMo');
    fireEvent.change(momoAmt, { target: { value: '50000' } });
    const momoRef = screen.getByTestId('payment-ref-MTN MoMo');
    fireEvent.change(momoRef, { target: { value: 'aaa' } });

    const payBtn = screen.getByTestId('payment-submit-btn') as HTMLButtonElement;
    // Evil nested alternation quantifier MUST fail closed even when input matches!
    expect(payBtn.disabled).toBe(true);

    // Switching to safe alternation-without-repeat pattern accepts valid input
    fireEvent.change(momoAmt, { target: { value: '0' } });
    fireEvent.change(screen.getByTestId('payment-amount-Safe Card'), { target: { value: '50000' } });
    fireEvent.change(screen.getByTestId('payment-ref-Safe Card'), { target: { value: 'MM-123456' } });
    expect(payBtn.disabled).toBe(false);
  });

  it('[Minor #3] useCaptainContext does not inherit another user cached captain context when offline', async () => {
    const waiter1Ctx = {
      user: 'waiter1@hotel.test',
      roles: ['URY Captain'],
      branch: 'Kampala Branch',
      rooms: [{ name: 'Main Hall', branch: 'Kampala Branch' }],
      pos_profile: {
        name: 'Main Till',
        transfer_role_permissions: false,
        role_allowed_for_billing: false,
        role_restricted_for_table_order: false,
        remove_items: false,
        show_image: false,
        custom_enable_kot_reprint: false,
        custom_enable_multiple_cashier: false,
      },
      role_restricted_for_table_order: false,
      opening_state: { pos_open: true },
      reservation_enabled: false,
    };

    const Harness = () => {
      const { context, isLoading, error } = useCaptainContext();
      if (isLoading) return <div data-testid="ctx-state">loading</div>;
      return (
        <div>
          <div data-testid="ctx-user">{context?.user ?? 'NONE'}</div>
          <div data-testid="ctx-error">{error ?? ''}</div>
        </div>
      );
    };

    // Phase 1: waiter1 loads online -> populates cache + last key
    useRootStore.setState({ user: { name: 'waiter1@hotel.test', roles: ['URY Captain'] } });
    vi.spyOn(call, 'get').mockResolvedValueOnce({ message: waiter1Ctx });

    const first = render(<Harness />);
    await waitFor(() => {
      expect(screen.getByTestId('ctx-user').textContent).toBe('waiter1@hotel.test');
    });
    first.unmount();

    // Phase 2: waiter2 logs in on the same device and goes offline with NO cache for waiter2
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
    useRootStore.setState({ user: { name: 'waiter2@hotel.test', roles: ['URY Captain'] } });
    vi.spyOn(call, 'get').mockRejectedValueOnce(new Error('Network offline'));

    const second = render(<Harness />);
    await waitFor(() => {
      expect(screen.getByTestId('ctx-user').textContent).toBe('NONE');
    });
    expect(screen.getByTestId('ctx-error').textContent).toBeTruthy();
    second.unmount();

    // Phase 3: waiter1 offline STILL gets waiter1's own cached context
    useRootStore.setState({ user: { name: 'waiter1@hotel.test', roles: ['URY Captain'] } });
    vi.spyOn(call, 'get').mockRejectedValueOnce(new Error('Network offline'));

    render(<Harness />);
    await waitFor(() => {
      expect(screen.getByTestId('ctx-user').textContent).toBe('waiter1@hotel.test');
    });
    expect(screen.getByTestId('ctx-error').textContent).toBe('');
  });
});
