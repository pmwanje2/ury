import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { call, db } from '@ury/core';
import { initI18n } from '../i18n';
import { getCombinedPosProfile } from '../lib/pos-profile-api';
import { useRootStore } from '../store/root-store';
import { usePOSStore } from '../store/pos-store';
import AuthGuard from './AuthGuard';
import POSOpeningScreen from './POSOpeningScreen';
import POSClosingDialog from './POSClosingDialog';
import ShiftReportDialog from './ShiftReportDialog';
import type { AvailableOutlet } from '../lib/outlet-api';

const rootInit = useRootStore.getState();
const posInit = usePOSStore.getState();

const TWO_OUTLETS: AvailableOutlet[] = [
  {
    pos_profile: 'Main Grill POS',
    name: 'Main Grill POS',
    branch: 'Main Grill',
    company: 'Maximus Hospitality',
    restaurant: 'Main Grill',
    currency: 'UGX',
    has_open_shift: false,
    open_entry: null,
    is_active: false,
  },
  {
    pos_profile: 'Pool Bar POS',
    name: 'Pool Bar POS',
    branch: 'Pool Bar',
    company: 'Maximus Hospitality',
    restaurant: 'Pool Bar',
    currency: 'USD',
    has_open_shift: false,
    open_entry: null,
    is_active: false,
  },
];

beforeEach(async () => {
  sessionStorage.clear();
  localStorage.clear();
  await initI18n('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useRootStore.setState(rootInit, true);
  usePOSStore.setState(posInit, true);
});

describe('Hydrogen R1 fixes (P0-1, P1-4, P1-5, P2-3)', () => {
  it('[P0-1] getCombinedPosProfile never calls getPosProfileFull(undefined) on requires_outlet_selection', async () => {
    const getSpy = vi.spyOn(call, 'get').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.api.getPosProfile') {
        return Promise.resolve({
          message: {
            requires_outlet_selection: true,
            available_outlets: TWO_OUTLETS,
          },
        });
      }
      if (method === 'ury.ury_pos.api.getPosProfileFull') {
        return Promise.reject(new Error('HTTP 500: getPosProfileFull(undefined) must never be called'));
      }
      return Promise.resolve({ message: {} });
    });

    await expect(getCombinedPosProfile()).rejects.toMatchObject({
      requiresOutletSelection: true,
      availableOutlets: TWO_OUTLETS,
    });
    expect(
      getSpy.mock.calls.some(([method]) => method === 'ury.ury_pos.api.getPosProfileFull'),
    ).toBe(false);
  });

  it('[P0-1] AuthGuard cold boot with requires_outlet_selection renders mandatory OutletPickerDialog (no Access Denied) and hydrates POS on pick', async () => {
    const user = userEvent.setup();
    let selectedOutlet: string | null = null;

    const getSpy = vi.spyOn(call, 'get').mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === 'ury.ury_pos.api.getPosProfile') {
        if (!selectedOutlet) {
          return Promise.resolve({
            message: {
              requires_outlet_selection: true,
              available_outlets: TWO_OUTLETS,
            },
          });
        }
        return Promise.resolve({
          message: {
            pos_profile: selectedOutlet,
            branch: 'Pool Bar',
            company: 'Maximus Hospitality',
            currency: 'USD',
            country: 'Uganda',
            waiter: 'cashier.multi@example.com',
            cashier: 'cashier.multi@example.com',
            owner: 'cashier.multi@example.com',
            warehouse: 'Pool Stores',
            print_format: 'Receipt',
            qz_print: 0,
            qz_host: null,
            printer: null,
            print_type: 'Network',
            tableAttention: 10,
            paid_limit: 0,
            disable_rounded_total: 0,
            enable_discount: 1,
            multiple_cashier: 0,
            enable_payment_without_printing: 0,
            requires_outlet_selection: false,
            available_outlets_count: 2,
          },
        });
      }
      if (method === 'ury.ury_pos.api.getPosProfileFull') {
        const profileName = params?.pos_profile;
        if (!profileName) {
          return Promise.reject(new Error('HTTP 500: pos_profile is undefined'));
        }
        return Promise.resolve({
          message: {
            name: profileName,
            company: 'Maximus Hospitality',
            country: 'Uganda',
            currency: 'USD',
            branch: 'Pool Bar',
            restaurant: 'Pool Bar',
            warehouse: 'Pool Stores',
            disabled: 0,
            role_allowed_for_billing: [{ role: 'URY Cashier' }],
            role_restricted_for_table_order: [],
            transfer_role_permissions: [],
            enable_payment_without_printing: 0,
          },
        });
      }
      if (method === 'ury.ury_pos.outlet_context.get_available_outlets') {
        return Promise.resolve({ message: TWO_OUTLETS });
      }
      if (method === 'ury.ury_pos.api.getRestaurantMenu') {
        return Promise.resolve({ message: { items: [] } });
      }
      if (method === 'ury.ury_pos.api.getMenuCourses') {
        return Promise.resolve({ message: [] });
      }
      if (method === 'ury.ury_pos.api.getModeOfPayment') {
        return Promise.resolve({ message: ['Cash'] });
      }
      return Promise.resolve({ message: {} });
    });

    const postSpy = vi.spyOn(call, 'post').mockImplementation((method: string, body?: Record<string, unknown>) => {
      if (method === 'ury.ury_pos.outlet_context.set_active_outlet') {
        selectedOutlet = String(body?.pos_profile || '');
        return Promise.resolve({
          message: {
            active_outlet: selectedOutlet,
            pos_profile: selectedOutlet,
            available_outlets: TWO_OUTLETS,
          },
        });
      }
      return Promise.resolve({ message: {} });
    });

    vi.spyOn(db, 'getDoc').mockResolvedValue({ name: 'USD', symbol: '$' } as never);

    vi.stubGlobal('indexedDB', new IDBFactory());
    // Seed authenticated user (skipping checkAuth redirect)
    useRootStore.setState({
      user: {
        name: 'cashier.multi@example.com',
        full_name: 'Multi Cashier',
        roles: ['URY Cashier'],
      },
      isLoading: false,
      error: null,
      posProfile: null,
      hasAccess: false,
      checkAuth: vi.fn().mockResolvedValue(undefined),
    });

    render(
      <AuthGuard>
        <div data-testid="pos-shell-ready">POS Loaded</div>
      </AuthGuard>,
    );

    // Mandatory outlet picker must render cleanly without Access Denied or getPosProfileFull(undefined)
    expect(await screen.findByText('Select Outlet')).toBeInTheDocument();
    expect(screen.getByText('Main Grill POS')).toBeInTheDocument();
    expect(screen.getByText('Pool Bar POS')).toBeInTheDocument();
    expect(screen.queryByText(/Access Denied/i)).not.toBeInTheDocument();
    expect(
      getSpy.mock.calls.some(([method]) => method === 'ury.ury_pos.api.getPosProfileFull'),
    ).toBe(false);

    // Pick Pool Bar POS -> switches active outlet, hydrates profile, and renders POS children seamlessly!
    await user.click(screen.getByRole('button', { name: /Pool Bar POS/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.outlet_context.set_active_outlet',
        { pos_profile: 'Pool Bar POS' },
      ),
    );
    expect(await screen.findByTestId('pos-shell-ready')).toBeInTheDocument();
    expect(useRootStore.getState().posProfile?.name).toBe('Pool Bar POS');
    expect(usePOSStore.getState().posProfile?.name).toBe('Pool Bar POS');
  });

  it('[P1-4] POSOpeningScreen renders TimeclockButton so clocked-out cashiers can clock in before opening a shift', async () => {
    const user = userEvent.setup();
    useRootStore.setState({
      user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] },
    });

    vi.spyOn(call, 'get').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.api.get_pos_opening_screen_data') {
        return Promise.resolve({
          message: {
            company: 'Maximus Hospitality',
            company_currency: 'UGX',
            branch: 'Main Grill',
            restaurant: 'Main Grill',
            user: 'cashier@example.com',
            user_full_name: 'Grace Cashier',
            selected_profile: 'Main Grill POS',
            allowed_profiles: [{ name: 'Main Grill POS', label: 'Main Grill POS' }],
            payment_modes: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
            daily_close_pending: false,
            multi_cashier: { enabled: false, main_cashier_open: false, main_cashier_configured: false },
            permissions: { create: true, submit: true },
          },
        });
      }
      if (method === 'ury.ury_pos.timeclock.get_my_timeclock_status') {
        return Promise.resolve({
          message: {
            available: true,
            has_employee: true,
            employee: 'HR-EMP-0001',
            employee_name: 'Grace Cashier',
            is_clocked_in: false,
            last_log_type: null,
            last_punch_time: null,
            clocked_in_since: null,
            elapsed_minutes: 0,
            shift: null,
            open_checks_count: 0,
            open_checks: [],
            has_open_shift: false,
            open_shift: null,
            block_clock_out_with_open_work: true,
            require_clock_in_to_order: true,
          },
        });
      }
      return Promise.resolve({ message: {} });
    });

    const postSpy = vi.spyOn(call, 'post').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.timeclock.punch_timeclock') {
        return Promise.resolve({
          message: {
            available: true,
            has_employee: true,
            employee: 'HR-EMP-0001',
            employee_name: 'Grace Cashier',
            is_clocked_in: true,
            last_log_type: 'IN',
            last_punch_time: '2026-11-24 08:15:00',
            clocked_in_since: '2026-11-24 08:15:00',
            elapsed_minutes: 0,
            shift: 'Hospitality All Day',
            open_checks_count: 0,
            open_checks: [],
            has_open_shift: false,
            open_shift: null,
            block_clock_out_with_open_work: true,
            require_clock_in_to_order: true,
          },
        });
      }
      return Promise.resolve({ message: {} });
    });

    render(<POSOpeningScreen />);

    const tcBtn = await screen.findByTestId('timeclock-button');
    expect(tcBtn).toBeInTheDocument();
    expect(tcBtn.textContent).toMatch(/Clock In/i);

    await user.click(tcBtn);
    await user.click(await screen.findByRole('button', { name: /Confirm Clock In/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('ury.ury_pos.timeclock.punch_timeclock', {
        action: 'IN',
        pos_profile: 'Main Grill POS',
      }),
    );
    expect(await screen.findByRole('button', { name: /08:15/i })).toBeInTheDocument();
  });

  it('[P1-5 + #151/#157] POSClosingDialog pins footer in flex-col modal and submits tip_declarations via controlled close', async () => {
    useRootStore.setState({
      user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] },
    });
    usePOSStore.setState({
      paymentModes: ['Cash'],
      posProfile: {
        name: 'Main Grill POS',
        company: 'Maximus Hospitality',
        currency: 'UGX',
        owner: 'cashier@example.com',
        multiple_cashier: 0,
        custom_blind_cash_count: 0,
        over_short_tolerance: 0,
        require_tip_declaration: 0,
        payment_mode_meta: { Cash: { type: 'Cash', overtender_rule: 'Refuse' } },
      } as unknown as ReturnType<typeof usePOSStore.getState>['posProfile'],
    });

    vi.spyOn(db, 'getDoc').mockResolvedValue({
      name: 'POS-OPE-1',
      balance_details: [{ mode_of_payment: 'Cash', opening_amount: 50000 }],
    } as never);

    vi.spyOn(call, 'get').mockImplementation((method: string) => {
      if (method === 'ury.ury_pos.till_controls.list_open_tills' || method === 'ury.ury_pos.api.get_open_pos_opening_entries') {
        return Promise.resolve({
          message: [
            {
              name: 'POS-OPE-1',
              user: 'cashier@example.com',
              user_name: 'Grace Cashier',
              period_start_date: '2026-11-24 08:00:00',
              pos_profile: 'Main Grill POS',
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
              name: 'POS-INV-1',
              grand_total: 50000,
              net_total: 50000,
              total_qty: 1,
              change_amount: 0,
              account_for_change_amount: 'Cash - MH',
              taxes: [],
              payments: [{ mode_of_payment: 'Cash', amount: 50000, account: 'Cash - MH' }],
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
            pos_profile: 'Main Grill POS',
            pos_opening_entry: 'POS-OPE-1',
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
      if (method === 'ury.ury_pos.tips_declaration.get_shift_tip_declaration_rows') {
        return Promise.resolve({
          message: {
            pos_opening_entry: 'POS-OPE-1',
            pos_profile: 'Main Grill POS',
            currency: 'UGX',
            currency_precision: 0,
            require_tip_declaration: false,
            rows: [
              {
                user: 'waiter1@example.com',
                employee: 'HR-EMP-001',
                full_name: 'Alice Nakato',
                currency: 'UGX',
                charged_tips_collected: 5000,
                tips_paid_from_till: 0,
                net_tips_payable: 5000,
                declared_cash_tips: 0,
                remarks: '',
              },
            ],
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
        return Promise.resolve({ message: { name: 'POS-CLO-1', docstatus: 1 } });
      }
      return Promise.resolve({ message: {} });
    });

    render(<POSClosingDialog open onOpenChange={vi.fn()} />);

    expect(await screen.findByText('Alice Nakato')).toBeInTheDocument();

    // Flex-column dialog with pinned footer & inner scroll body so 1024x768 never clips buttons
    const scrollBody = screen.getByTestId('pos-closing-scroll-body');
    expect(scrollBody.className).toContain('flex-1');
    expect(scrollBody.className).toContain('min-h-0');
    expect(scrollBody.className).toContain('overflow-y-auto');

    const dialogContent = scrollBody.parentElement as HTMLElement;
    expect(dialogContent.className).toContain('flex');
    expect(dialogContent.className).toContain('flex-col');
    expect(dialogContent.className).toContain('overflow-hidden');
    expect(dialogContent.className).not.toContain('overflow-y-auto');

    const submitBtn = screen.getByRole('button', { name: /^Close POS$/i });
    expect(scrollBody.contains(submitBtn)).toBe(false);

    // Fill exact cash count 100000 and declare 20000 cash tips
    const [cashInput] = screen.getAllByRole('spinbutton');
    fireEvent.change(cashInput, { target: { value: '100000' } });
    const tipInput = screen.getByLabelText(/Alice Nakato/i);
    fireEvent.change(tipInput, { target: { value: '20000' } });

    fireEvent.click(submitBtn);
    fireEvent.click(await screen.findByRole('button', { name: /Yes, Close POS/i }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        'ury.ury_pos.till_controls.close_till_with_controls',
        expect.objectContaining({
          pos_opening_entry: 'POS-OPE-1',
          tip_declarations: [
            expect.objectContaining({
              user: 'waiter1@example.com',
              employee: 'HR-EMP-001',
              declared_cash_tips: 20000,
            }),
          ],
        }),
      ),
    );
  });

  it('[P2-3] ShiftReportDialog table uses unclipped horizontal scrolling with flex-col layout and scroll hint', async () => {
    const columns = [
      { fieldname: 'opening', label: 'Opening Entry', fieldtype: 'Data' },
      { fieldname: 'cashier', label: 'Cashier', fieldtype: 'Data' },
      { fieldname: 'gross', label: 'Gross Sales', fieldtype: 'Currency' },
      { fieldname: 'discount_breakdown', label: 'Discount Breakdown', fieldtype: 'Currency' },
      { fieldname: 'returns', label: 'Returns', fieldtype: 'Currency' },
      { fieldname: 'voids', label: 'Voids', fieldtype: 'Currency' },
      { fieldname: 'expected', label: 'Expected Cash', fieldtype: 'Currency' },
      { fieldname: 'counted', label: 'Counted Cash', fieldtype: 'Currency' },
      { fieldname: 'variance', label: 'Variance', fieldtype: 'Currency' },
    ];
    vi.spyOn(call, 'get').mockResolvedValue({
      message: {
        available: true,
        has_open_shift: true,
        report_type: 'X',
        pos_profile: 'Main Grill POS',
        pos_opening_entry: 'POS-OPE-1',
        business_date: '2026-11-24',
        currency: 'UGX',
        columns,
        rows: [
          {
            opening: 'POS-OPE-1',
            cashier: 'Grace Cashier',
            gross: 55000,
            discount_breakdown: 0,
            returns: 0,
            voids: 0,
            expected: 105000,
            counted: 105000,
            variance: 0,
          },
        ],
        report_summary: [{ label: 'Net Sales', value: 55000, datatype: 'Currency' }],
      },
    });

    render(
      <ShiftReportDialog
        open={true}
        onOpenChange={vi.fn()}
        posProfile="Main Grill POS"
        posOpeningEntry="POS-OPE-1"
      />,
    );

    expect(await screen.findByText('Discount Breakdown')).toBeInTheDocument();
    const tableScroll = screen.getByTestId('shift-report-table-scroll');
    expect(tableScroll.className).toContain('overflow-x-auto');
    const tableEl = tableScroll.querySelector('table');
    expect(tableEl?.className).toContain('min-w-max');

    // Dialog uses flex-column with scrollable body & pinned footer
    const dialogBody = screen.getByTestId('shift-report-scroll-body');
    expect(dialogBody.className).toContain('flex-1');
    expect(dialogBody.className).toContain('min-h-0');
    expect(dialogBody.className).toContain('overflow-y-auto');
    expect(screen.getByTestId('shift-report-scroll-hint')).toBeInTheDocument();
  });
});
