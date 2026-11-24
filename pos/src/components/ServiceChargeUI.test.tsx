import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import en from '../i18n/locales/en.json';
import fr from '../i18n/locales/fr.json';
import es from '../i18n/locales/es.json';
import ar from '../i18n/locales/ar.json';

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
    formatCurrency: (val: number, cur = 'UGX') => {
      const digits = cur === 'UGX' || cur === 'JPY' ? 0 : cur === 'BHD' || cur === 'OMR' || cur === 'KWD' ? 3 : 2;
      return `${cur} ${Number(val || 0).toFixed(digits)}`;
    },
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

vi.mock('./ManagerApproval', () => ({
  default: () => null,
}));

const mockPosStoreState: Record<string, unknown> = {
  activeOrders: [
    {
      uniqueId: 'u-1',
      id: 'STEAK',
      item: 'STEAK',
      name: 'Grilled Steak',
      price: 50000,
      quantity: 2,
    },
  ],
  removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(),
  clearOrder: vi.fn(),
  setSelectedItem: vi.fn(),
  orderLoading: false,
  isOrderInteractionDisabled: () => false,
  isUpdatingOrder: false,
  posProfile: {
    name: 'Main Till',
    company: 'Test Co',
    currency: 'UGX',
    cashier: 'cashier@example.com',
    owner: 'cashier@example.com',
    custom_service_charge_rules: [
      {
        order_type: 'Dine In',
        enabled: 1,
        charge_mode: 'Percentage',
        rate: 10,
        fixed_amount: 0,
        min_amount_floor: 0,
        min_pax: 6,
        calculation_basis: 'Post-Discount Subtotal',
        taxable: 1,
        account_head: 'Service Charge Payable - TC',
        description: 'Service Charge',
      },
      {
        order_type: 'Room Service',
        enabled: 1,
        charge_mode: 'Fixed Amount',
        rate: 0,
        fixed_amount: 10000,
        min_amount_floor: 0,
        min_pax: 0,
        calculation_basis: 'Fixed Amount',
        taxable: 0,
        account_head: 'Service Charge Income - TC',
        description: 'Room Tray Fee',
      },
    ],
  },
  currency: 'UGX',
  selectedOrderType: 'Dine In',
  selectedTable: 'T1',
  selectedRoom: 'Main',
  tableOrder: null,
  editCustomer: null,
  selectedCustomer: { id: 'Walk In', name: 'Walk In' },
  selectedAggregator: null,
  resetOrderState: vi.fn(),
  paymentModes: ['Cash'],
  orderId: null,
  originalSentItems: [],
  orderComment: '',
  setOrderComment: vi.fn(),
  guestLabel: '',
  setGuestLabel: vi.fn(),
  noOfPax: 6,
  setNoOfPax: vi.fn(),
  lastModifiedTime: null,
};

vi.mock('../store/pos-store', () => ({
  usePOSStore: () => mockPosStoreState,
}));

vi.mock('../store/root-store', () => ({
  useRootStore: (selector?: (s: { user: { name: string; roles: string[] } }) => unknown) => {
    const state = { user: { name: 'cashier@example.com', roles: ['URY Cashier'] } };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('./CustomerSelect', () => ({ CustomerSelect: () => <div /> }));
vi.mock('./OrderTypeSelect', () => ({ default: () => <div /> }));
vi.mock('./ProductDialog', () => ({ default: () => null }));
vi.mock('./CommentDialog', () => ({ default: () => null }));
vi.mock('./HoldFireControls', () => ({ HoldFireControls: () => null }));
vi.mock('../offline/use-outbox', () => ({
  useOutbox: () => ({ queue: false, offline: false, replaying: false, rows: [] }),
}));

describe('G19 Service Charge UI, Rule Matching, Precision (0/2/3dp) & 4-Locale i18n', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPosStoreState.selectedOrderType = 'Dine In';
    mockPosStoreState.noOfPax = 6;
  });

  afterEach(() => {
    cleanup();
  });

  it('resolves order-type/pax rules, floor, Fixed Amount, legacy fallback, and 0/2/3dp precision', async () => {
    const sc = await import('../lib/service-charge');

    const rules = [
      {
        order_type: '*',
        enabled: 1,
        charge_mode: 'Percentage' as const,
        rate: 3,
        fixed_amount: 0,
        min_amount_floor: 0,
        min_pax: 0,
        calculation_basis: 'Post-Discount Subtotal' as const,
        taxable: 0,
        account_head: 'SC - TC',
        description: 'Default SC',
      },
      {
        order_type: 'Dine In',
        enabled: 1,
        charge_mode: 'Percentage' as const,
        rate: 10,
        fixed_amount: 0,
        min_amount_floor: 15000,
        min_pax: 6,
        calculation_basis: 'Pre-Discount Subtotal' as const,
        taxable: 1,
        account_head: 'SC - TC',
        description: 'Large Party SC',
      },
      {
        order_type: 'Room Service',
        enabled: 1,
        charge_mode: 'Fixed Amount' as const,
        rate: 0,
        fixed_amount: 12.345,
        min_amount_floor: 0,
        min_pax: 0,
        calculation_basis: 'Fixed Amount' as const,
        taxable: 0,
        account_head: 'SC - TC',
        description: 'Tray Fee',
      },
    ];

    // Exact match Dine In @ 6 pax beats wildcard
    const r6 = sc.resolveActiveServiceChargeRule({ custom_service_charge_rules: rules }, 'Dine In', 6);
    expect(r6?.description).toBe('Large Party SC');
    // Option 17 floor binds on 100,000 UGX (10% = 10,000 < 15,000)
    expect(sc.estimateServiceCharge(100000, r6, 'UGX')).toBe(15000);
    // 0dp / 2dp / 3dp precision
    expect(sc.currencyPrecision('UGX')).toBe(0);
    expect(sc.currencyPrecision('USD')).toBe(2);
    expect(sc.currencyPrecision('BHD')).toBe(3);
    expect(sc.currencyInputStep('UGX')).toBe('1');
    expect(sc.currencyInputStep('USD')).toBe('0.01');
    expect(sc.currencyInputStep('BHD')).toBe('0.001');

    // Room Service 3dp BHD keeps 3 decimal places
    const rRoom = sc.resolveActiveServiceChargeRule({ custom_service_charge_rules: rules }, 'Room Service', 1);
    expect(sc.estimateServiceCharge(50, rRoom, 'BHD')).toBe(12.345);

    // Below min_pax falls back to wildcard
    const r2 = sc.resolveActiveServiceChargeRule({ custom_service_charge_rules: rules }, 'Dine In', 2);
    expect(r2?.description).toBe('Default SC');

    // Legacy scalar fallback when rules array empty
    const legacy = sc.resolveActiveServiceChargeRule(
      {
        custom_service_charge_rules: [],
        custom_service_charge_rate: 8,
        custom_service_charge_account: 'SC - TC',
        custom_service_charge_min_pax: 4,
        custom_service_charge_order_types: 'Dine In',
        custom_service_charge_description: 'Legacy SC',
      },
      'Dine In',
      4,
    );
    expect(legacy?.rate).toBe(8);
    expect(legacy?.description).toBe('Legacy SC');

    // Tax classification uses custom_pos_charge_kind first (keeps Service Charge Tax in taxRows!)
    const classified = sc.classifyInvoiceTaxes([
      { description: 'Output VAT 18%', rate: 18000, custom_pos_charge_kind: '' },
      { description: 'Gratuity', rate: 10000, custom_pos_charge_kind: 'Service Charge' },
      { description: 'Output VAT 18% on Service Charge', rate: 1800, custom_pos_charge_kind: 'Service Charge Tax' },
      { description: 'Charged Tip', rate: 5000, custom_pos_charge_kind: 'Charged Tip' },
    ]);
    expect(classified.serviceChargeRows).toHaveLength(1);
    expect(classified.serviceChargeRows[0].description).toBe('Gratuity');
    expect(classified.taxRows).toHaveLength(2);
    expect(classified.tipRows).toHaveLength(1);
  });

  it('OrderPanel shows Service Charge breakdown (rule/pax) and collapsible >=44px mobile summary bar', async () => {
    const { default: OrderPanel } = await import('./OrderPanel');
    const { rerender } = render(<OrderPanel />);

    // Desktop breakdown shows Service Charge line with 10% and 6+ pax metadata
    expect(screen.getByTestId('order-panel-sc-row')).toBeInTheDocument();
    expect(screen.getByTestId('order-panel-sc-row').textContent).toMatch(/10%/);
    expect(screen.getByTestId('order-panel-sc-row').textContent).toMatch(/6\+/);

    // Collapsible mobile summary bar button meets >=44px (min-h-11) and toggles expanded details
    const toggle = screen.getByTestId('mobile-summary-toggle');
    expect(toggle.className).toContain('min-h-11');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');

    // Dropping pax below min_pax=6 hides Service Charge row cleanly
    mockPosStoreState.noOfPax = 2;
    rerender(<OrderPanel />);
    expect(screen.queryByTestId('order-panel-sc-row')).not.toBeInTheDocument();
  });

  it('CheckPrivilegedDialog shows active service charge rule details and currency-precision step', async () => {
    callGetMock.mockImplementation((method: string) => {
      if (method.includes('get_service_charge_status')) {
        return Promise.resolve({
          message: {
            invoice: 'INV-1',
            waived: false,
            service_charge_amount: 10000,
            service_charge_tax_amount: 1800,
            matched_rule: {
              order_type: 'Dine In',
              charge_mode: 'Percentage',
              rate: 10,
              fixed_amount: 0,
              min_pax: 6,
              calculation_basis: 'Pre-Discount Subtotal',
              taxable: 1,
              description: 'Large Party SC',
            },
          },
        });
      }
      return Promise.resolve({
        message: [{ code: 'VIP_COMP', description: 'VIP guest waiver', requires_note: 0 }],
      });
    });

    const { default: CheckPrivilegedDialog } = await import('./CheckPrivilegedDialog');
    render(
      <CheckPrivilegedDialog
        open
        onOpenChange={() => {}}
        initialTab="service_charge"
        posProfile="Main Till"
        company="Test Co"
        invoiceName="INV-1"
        currency="BHD"
        onSuccess={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByTestId('sc-active-rule-card')).toBeInTheDocument());
    expect(screen.getByTestId('sc-active-rule-card').textContent).toContain('Large Party SC');
    expect(screen.getByTestId('sc-active-rule-card').textContent).toContain('10%');

    // Switch to override mode and confirm BHD 3dp step="0.001"
    fireEvent.change(screen.getByLabelText(/service charge action/i), { target: { value: 'override' } });
    const amtInput = screen.getByLabelText(/fixed amount/i) as HTMLInputElement;
    expect(amtInput.step).toBe('0.001');
  });

  it('ships complete 4-locale i18n keys for G19 Service Charge UI with zero vendor leaks', () => {
    const requiredKeys = [
      'sc_active_rule',
      'sc_waived_badge',
      'sc_fixed_label',
      'sc_taxable_badge',
      'sc_pre_discount',
      'sc_post_discount',
      'sc_pax_threshold',
      'sc_summary_net',
      'sc_summary_discount',
      'sc_summary_tax',
      'sc_summary_toggle',
    ];
    for (const [lang, locale] of [
      ['en', en],
      ['fr', fr],
      ['es', es],
      ['ar', ar],
    ] as const) {
      const section = (locale as Record<string, Record<string, string>>).privileged_actions || {};
      for (const key of requiredKeys) {
        expect(section[key], `Missing ${lang}.privileged_actions.${key}`).toBeTruthy();
        expect(section[key]).not.toMatch(/\b(URY|HUF|Kamra|Tridz|Simphony)\b/i);
      }
    }
  });
});
