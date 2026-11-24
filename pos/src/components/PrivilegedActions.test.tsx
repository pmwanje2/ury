import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import en from '../i18n/locales/en.json';
import fr from '../i18n/locales/fr.json';
import es from '../i18n/locales/es.json';
import ar from '../i18n/locales/ar.json';
import { availabilityPermissions } from '@ury/core/availability';

const callGetMock = vi.fn();
const callPostMock = vi.fn();
const showToastSuccess = vi.fn();
const showToastError = vi.fn();

vi.mock('@ury/core', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@ury/core');
  return {
    ...actual,
    call: {
      get: (...args: unknown[]) => callGetMock(...args),
      post: (...args: unknown[]) => callPostMock(...args),
    },
    formatCurrency: (val: number, cur = 'UGX') => `${cur} ${Number(val || 0).toLocaleString()}`,
    parseFrappeError: (err: unknown, fallback = 'Error') =>
      err instanceof Error ? err.message : typeof err === 'string' ? err : fallback,
  };
});

vi.mock('@ury/ui', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@ury/ui');
  return {
    ...actual,
    showToast: {
      success: (...args: unknown[]) => showToastSuccess(...args),
      error: (...args: unknown[]) => showToastError(...args),
      info: vi.fn(),
    },
  };
});

// Stub ManagerApproval so tests assert exact privilege/claim/token handoff without re-testing B2a internals
const managerApprovalPropsSpy = vi.fn();
vi.mock('./ManagerApproval', () => ({
  default: (props: {
    open: boolean;
    privilege: string;
    claim: Record<string, unknown>;
    onApproved: (res: { token: string; approverName: string; approver: string; reason?: string; note?: string }) => void;
    onCancel: () => void;
  }) => {
    if (!props.open) return null;
    managerApprovalPropsSpy(props);
    return (
      <div data-testid="mock-manager-approval" data-privilege={props.privilege}>
        <button
          type="button"
          data-testid="mock-approve-btn"
          onClick={() =>
            props.onApproved({
              token: `TOK-${props.privilege}`,
              approverName: 'Duty Manager',
              approver: 'manager@example.com',
              reason: 'GUEST_REQUEST',
              note: 'Approved at till',
            })
          }
        >
          Approve PIN
        </button>
        <button type="button" data-testid="mock-cancel-btn" onClick={props.onCancel}>
          Cancel PIN
        </button>
      </div>
    );
  },
}));

describe('Scope e+f Privileged POS Actions UI & Contracts (B5e+f)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window.navigator, 'onLine', {
      value: true,
      configurable: true,
    });
  });

  afterEach(() => {
    cleanup();
  });

  it('refuses every Scope e+f privileged API wrapper when offline without sending requests', async () => {
    Object.defineProperty(window.navigator, 'onLine', {
      value: false,
      configurable: true,
    });

    const api = await import('../lib/privileged-actions-api');
    const orderApi = await import('../lib/order-api');
    const availApi = await import('../lib/availability-api');
    const tableApi = await import('../lib/table-api');

    const calls: Array<() => Promise<unknown>> = [
      () => api.overrideLinePrice({ invoice: 'INV-1', itemRowName: 'L1', newRate: 40000, reason: 'PRICE_MATCH' }),
      () => api.addOpenItem({ invoice: 'INV-1', itemCode: 'OPEN-FOOD', qty: 1, rate: 50000, reference: 'Chef Fish' }),
      () => api.applyComplimentary({ invoice: 'INV-1', compType: 'Complimentary', reason: 'VIP_COMP' }),
      () => api.applyTaxExemption({ invoice: 'INV-1', reason: 'DIPLOMAT', certificate: 'DIP-01', taxCategory: 'Exempt' }),
      () => api.adjustServiceCharge({ invoice: 'INV-1', mode: 'waive', reason: 'GUEST_COMPLAINT' }),
      () => api.adjustClosedCheckTip({ invoice: 'INV-1', newTipAmount: 5000, reason: 'TIP_SLIP' }),
      () => api.transferCheckOutlet({ invoice: 'INV-1', targetPosProfile: 'Rooftop Till' }),
      () => api.pickupOtherCheck({ invoice: 'INV-1' }),
      () => orderApi.reprintKot('INV-1'),
      () => tableApi.mergeTablesBatch('T1', ['T2']),
      () => availApi.setItemAvailability('Main', 'FISH', { status: 'Available' }),
      () => tableApi.updateTableReservationStatus('RES-1', 'Cancelled', undefined, 'GUEST_CANCELLED'),
    ];

    for (const invoke of calls) {
      await expect(invoke()).rejects.toThrow();
    }
    expect(callPostMock).not.toHaveBeenCalled();
  });

  it('PriceOverrideDialog escalates to ManagerApproval on POS-AUTH-REQUIRED and retries with token', async () => {
    callGetMock.mockResolvedValue({
      message: [{ code: 'PRICE_MATCH', description: 'Price match', requires_note: 0 }],
    });
    callPostMock
      .mockRejectedValueOnce(new Error('[POS-AUTH-REQUIRED] A manager must approve this action.'))
      .mockResolvedValueOnce({ message: { invoice: 'INV-1', rate: 35000 } });

    const { default: PriceOverrideDialog } = await import('./PriceOverrideDialog');
    const onSuccess = vi.fn();

    render(
      <PriceOverrideDialog
        open
        mode="override"
        posProfile="Main Till"
        invoiceName="INV-1"
        itemRowName="ROW-1"
        itemName="Ribeye Steak"
        currentRate={50000}
        currency="UGX"
        onSuccess={onSuccess}
        onOpenChange={vi.fn()}
      />,
    );

    const rateInput = await screen.findByLabelText(/new price|override price/i);
    fireEvent.change(rateInput, { target: { value: '35000' } });

    const reasonSelect = await screen.findByLabelText(/reason/i);
    fireEvent.change(reasonSelect, { target: { value: 'PRICE_MATCH' } });

    fireEvent.click(screen.getByRole('button', { name: /apply|save|confirm/i }));

    await waitFor(() => {
      expect(screen.getByTestId('mock-manager-approval')).toHaveAttribute('data-privilege', 'price_override');
    });

    fireEvent.click(screen.getByTestId('mock-approve-btn'));

    await waitFor(() => {
      expect(callPostMock).toHaveBeenCalledTimes(2);
      expect(callPostMock).toHaveBeenLastCalledWith(
        'ury.ury_pos.pricing_actions.override_line_price',
        expect.objectContaining({
          invoice: 'INV-1',
          item_row_name: 'ROW-1',
          new_rate: 35000,
          approval_token: 'TOK-price_override',
        }),
      );
      expect(onSuccess).toHaveBeenCalled();
    });
  });

  it('PriceOverrideDialog open-item mode sends dish reference and escalates on over-HALO PIN', async () => {
    callGetMock.mockResolvedValue({ message: [] });
    callPostMock
      .mockRejectedValueOnce(new Error('[POS-AUTH-REQUIRED] Exceeds max open item price.'))
      .mockResolvedValueOnce({ message: { invoice: 'INV-1', rate: 250000 } });

    const { default: PriceOverrideDialog } = await import('./PriceOverrideDialog');
    const onSuccess = vi.fn();

    render(
      <PriceOverrideDialog
        open
        mode="open_item"
        posProfile="Main Till"
        invoiceName="INV-1"
        itemCode="OPEN-FOOD"
        itemName="Market Special"
        currentRate={0}
        currency="UGX"
        onSuccess={onSuccess}
        onOpenChange={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/reference|dish name/i), { target: { value: 'Whole Red Snapper' } });
    fireEvent.change(screen.getByLabelText(/price/i), { target: { value: '250000' } });
    fireEvent.click(screen.getByRole('button', { name: /add|apply|confirm/i }));

    await waitFor(() => {
      expect(screen.getByTestId('mock-manager-approval')).toHaveAttribute('data-privilege', 'open_item');
    });

    fireEvent.click(screen.getByTestId('mock-approve-btn'));

    await waitFor(() => {
      expect(callPostMock).toHaveBeenLastCalledWith(
        'ury.ury_pos.pricing_actions.add_open_item',
        expect.objectContaining({
          invoice: 'INV-1',
          item_code: 'OPEN-FOOD',
          rate: 250000,
          reference: 'Whole Red Snapper',
          approval_token: 'TOK-open_item',
        }),
      );
      expect(onSuccess).toHaveBeenCalled();
    });
  });

  it('CheckPrivilegedDialog handles complimentary, tax exempt (native Tax Category), and service charge waive with ManagerApproval', async () => {
    callGetMock.mockImplementation((method: string) => {
      if (method.includes('list_tax_exemption_options')) {
        return Promise.resolve({
          message: {
            tax_categories: [{ name: 'Diplomatic Exempt', custom_efris_tax_category: '03' }],
            item_tax_templates: [{ name: 'Exempt 0%', custom_efris_tax_category: '03' }],
          },
        });
      }
      return Promise.resolve({
        message: [
          { code: 'VIP_COMP', description: 'VIP hospitality', requires_note: 0 },
          { code: 'DIPLOMAT', description: 'Diplomatic mission', requires_note: 0 },
          { code: 'GUEST_COMPLAINT', description: 'Guest complaint', requires_note: 0 },
        ],
      });
    });

    callPostMock
      .mockRejectedValueOnce(new Error('[POS-AUTH-REQUIRED] Second person required.'))
      .mockResolvedValueOnce({ message: { invoice: 'INV-1', comp_type: 'Complimentary' } })
      .mockRejectedValueOnce(new Error('[POS-AUTH-REQUIRED] Manager required.'))
      .mockResolvedValueOnce({ message: { invoice: 'INV-1', tax_category: 'Diplomatic Exempt' } })
      .mockRejectedValueOnce(new Error('[POS-AUTH-REQUIRED] Manager required.'))
      .mockResolvedValueOnce({ message: { invoice: 'INV-1', service_charge_waived: 1 } });

    const { default: CheckPrivilegedDialog } = await import('./CheckPrivilegedDialog');
    const onSuccess = vi.fn();

    render(
      <CheckPrivilegedDialog
        open
        initialTab="complimentary"
        posProfile="Main Till"
        company="Grand Hotel"
        invoiceName="INV-1"
        currency="UGX"
        onSuccess={onSuccess}
        onOpenChange={vi.fn()}
      />,
    );

    // 1. Complimentary tab
    const reasonEl = await screen.findByLabelText(/reason/i);
    fireEvent.change(reasonEl, { target: { value: 'VIP_COMP' } });
    fireEvent.click(screen.getByRole('button', { name: /apply complimentary|confirm/i }));

    await waitFor(() => {
      expect(screen.getByTestId('mock-manager-approval')).toHaveAttribute('data-privilege', 'complimentary');
    });
    fireEvent.click(screen.getByTestId('mock-approve-btn'));
    await waitFor(() => {
      expect(callPostMock).toHaveBeenCalledWith(
        'ury.ury_pos.pricing_actions.apply_complimentary',
        expect.objectContaining({ invoice: 'INV-1', approval_token: 'TOK-complimentary' }),
      );
    });

    // 2. Tax Exempt tab
    fireEvent.click(screen.getByRole('tab', { name: /tax exempt/i }));
    const taxCatSelect = await screen.findByLabelText(/tax category/i);
    fireEvent.change(taxCatSelect, { target: { value: 'Diplomatic Exempt' } });
    fireEvent.change(screen.getByLabelText(/certificate|reference/i), { target: { value: 'DIP-2026-99' } });
    fireEvent.change(screen.getByLabelText(/reason/i), { target: { value: 'DIPLOMAT' } });
    fireEvent.click(screen.getByRole('button', { name: /apply tax exempt|confirm/i }));

    await waitFor(() => {
      expect(screen.getByTestId('mock-manager-approval')).toHaveAttribute('data-privilege', 'tax_exempt');
    });
    fireEvent.click(screen.getByTestId('mock-approve-btn'));
    await waitFor(() => {
      expect(callPostMock).toHaveBeenCalledWith(
        'ury.ury_pos.pricing_actions.apply_tax_exemption',
        expect.objectContaining({
          invoice: 'INV-1',
          tax_category: 'Diplomatic Exempt',
          certificate: 'DIP-2026-99',
          approval_token: 'TOK-tax_exempt',
        }),
      );
    });

    // 3. Service Charge tab
    fireEvent.click(screen.getByRole('tab', { name: /service charge/i }));
    fireEvent.change(await screen.findByLabelText(/reason/i), { target: { value: 'GUEST_COMPLAINT' } });
    fireEvent.click(screen.getByRole('button', { name: /waive|apply service charge|confirm/i }));

    await waitFor(() => {
      expect(screen.getByTestId('mock-manager-approval')).toHaveAttribute('data-privilege', 'service_charge_waive');
    });
    fireEvent.click(screen.getByTestId('mock-approve-btn'));
    await waitFor(() => {
      expect(callPostMock).toHaveBeenCalledWith(
        'ury.ury_pos.pricing_actions.adjust_service_charge',
        expect.objectContaining({
          invoice: 'INV-1',
          mode: 'waive',
          approval_token: 'TOK-service_charge_waive',
        }),
      );
    });
  });

  it('TipAdjustDialog adjusts charged tip on closed check with ManagerApproval', async () => {
    callGetMock.mockResolvedValue({
      message: [{ code: 'TIP_SLIP', description: 'Signed card tip slip', requires_note: 0 }],
    });
    callPostMock
      .mockRejectedValueOnce(new Error('[POS-AUTH-REQUIRED] Manager required.'))
      .mockResolvedValueOnce({ message: { invoice: 'INV-CLOSED', custom_charged_tip: 15000, journal_entry: 'JE-1' } });

    const { default: TipAdjustDialog } = await import('./TipAdjustDialog');
    const onSuccess = vi.fn();

    render(
      <TipAdjustDialog
        open
        posProfile="Main Till"
        invoiceName="INV-CLOSED"
        currentTip={0}
        currency="UGX"
        onSuccess={onSuccess}
        onOpenChange={vi.fn()}
      />,
    );

    fireEvent.change(await screen.findByLabelText(/tip amount/i), { target: { value: '15000' } });
    fireEvent.change(await screen.findByLabelText(/reason/i), { target: { value: 'TIP_SLIP' } });
    fireEvent.click(screen.getByRole('button', { name: /save|apply|adjust/i }));

    await waitFor(() => {
      expect(screen.getByTestId('mock-manager-approval')).toHaveAttribute('data-privilege', 'tip_adjust');
    });
    fireEvent.click(screen.getByTestId('mock-approve-btn'));

    await waitFor(() => {
      expect(callPostMock).toHaveBeenLastCalledWith(
        'ury.ury_pos.pricing_actions.adjust_closed_check_tip',
        expect.objectContaining({
          invoice: 'INV-CLOSED',
          new_tip_amount: 15000,
          approval_token: 'TOK-tip_adjust',
        }),
      );
      expect(onSuccess).toHaveBeenCalled();
    });
  });

  it('availabilityPermissions exposes canRequestRestoreWithPin for floor roles while keeping canRestore false', () => {
    const captain = availabilityPermissions(['URY Captain']) as Record<string, boolean>;
    expect(captain.canMark).toBe(true);
    expect(captain.canRestore).toBe(false);
    expect(captain.canRequestRestoreWithPin).toBe(true);

    const manager = availabilityPermissions(['URY Manager']) as Record<string, boolean>;
    expect(manager.canMark).toBe(true);
    expect(manager.canRestore).toBe(true);
    expect(manager.canRequestRestoreWithPin).toBe(true);
  });

  it('all 4 locales define privileged_actions keys with zero forbidden vendor names', () => {
    const forbidden = /\b(URY|HUF|Kamra)\b/i;
    const requiredKeys = [
      'price_override',
      'open_item',
      'complimentary',
      'tax_exempt',
      'service_charge',
      'tip_adjust',
      'reprint_bill',
      'reprint_kot',
      'transfer_outlet',
      'pickup_other_check',
      'restore_86',
      'cancel_reservation',
    ];

    for (const [localeName, bundle] of Object.entries({ en, fr, es, ar })) {
      const section = (bundle as Record<string, Record<string, string>>).privileged_actions;
      expect(section, `${localeName} missing privileged_actions`).toBeDefined();
      for (const key of requiredKeys) {
        expect(section?.[key], `${localeName}.privileged_actions.${key} missing`).toBeTruthy();
        expect(section?.[key]).not.toMatch(forbidden);
      }
      // Check every string in privileged_actions for vendor brand leaks
      for (const [k, v] of Object.entries(section || {})) {
        expect(String(v), `${localeName}.privileged_actions.${k} leaks vendor name`).not.toMatch(forbidden);
      }
    }
  });
});
