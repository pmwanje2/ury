import React from 'react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BrowserRouter } from 'react-router-dom';
import en from '../src/i18n/locales/en.json';
import fr from '../src/i18n/locales/fr.json';
import es from '../src/i18n/locales/es.json';
import ar from '../src/i18n/locales/ar.json';

const { mockCallGet, mockCallPost, mockPosState, mockOutbox } = vi.hoisted(() => ({
  mockCallGet: vi.fn(),
  mockCallPost: vi.fn(),
  mockPosState: {
    value: {} as Record<string, unknown>,
  },
  mockOutbox: {
    value: {
      offline: false,
      replaying: false,
      queue: false,
      rows: [] as unknown[],
    },
  },
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useLocation: () => ({ pathname: '/register' }),
    useNavigate: () => vi.fn(),
  };
});

vi.mock('@ury/core', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@ury/core')>()),
  call: { get: mockCallGet, post: mockCallPost },
  formatCurrency: (amount: number, currency?: string) => `${currency || 'UGX'} ${Number(amount).toFixed(0)}`,
  isPrintStatusDisabled: () => true,
  logout: vi.fn().mockResolvedValue(undefined),
  parseFrappeError: (e: unknown, fallback = 'Error') => (e instanceof Error ? e.message : fallback),
  resolveBrand: (fb: { name: string; logo: string }) => fb,
  resolveDefaultCustomer: () => null,
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { name: 'cashier@example.com', full_name: 'Cashier One', roles: ['URY Cashier'] },
      orderSearchQuery: '',
      setOrderSearchQuery: vi.fn(),
    };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => mockPosState.value,
}));

vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: () => mockOutbox.value,
}));

vi.mock('../src/offline/cashier', () => ({
  assertOfflineActionAllowed: vi.fn().mockResolvedValue(undefined),
  clearCashierStorage: vi.fn().mockResolvedValue(undefined),
  submitOrderOfflineAware: vi.fn(),
  submitSettlementOfflineAware: vi.fn(),
}));

vi.mock('../src/lib/kot-status-api', () => ({
  getOrderKotStatus: vi.fn().mockResolvedValue({ items: {} }),
}));

vi.mock('../src/components/CustomerSelect', () => ({
  CustomerSelect: () => <div data-testid="customer-select">Customer</div>,
}));

vi.mock('../src/components/OrderTypeSelect', () => ({
  default: () => <div data-testid="order-type-select">Order Type</div>,
}));

vi.mock('../src/components/ProductDialog', () => ({
  default: () => null,
}));

vi.mock('../src/components/CommentDialog', () => ({
  default: () => null,
}));

vi.mock('../src/components/HoldFireControls', () => ({
  HoldFireControls: () => null,
}));

import Header from '../src/components/Header';
import MenuCard from '../src/components/MenuCard';
import OrderPanel from '../src/components/OrderPanel';
import PaymentDialog from '../src/components/PaymentDialog';
import { initI18n } from '../src/i18n';

const VENDOR = /\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala)\b/i;

function flattenLocale(tree: Record<string, unknown>, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(tree)) {
    if (k === '_meta') continue;
    if (typeof v === 'string') out[prefix + k] = v;
    else if (v && typeof v === 'object') Object.assign(out, flattenLocale(v as Record<string, unknown>, `${prefix}${k}.`));
  }
  return out;
}

describe('POS-PR-7: Header status strip, MenuCard daypart/promo, OrderPanel & PaymentDialog live preview', () => {
  beforeEach(async () => {
    await initI18n('en');
    vi.clearAllMocks();
    mockOutbox.value = { offline: false, replaying: false, queue: false, rows: [] };
    mockCallGet.mockResolvedValue({ message: { has_permission: false } });
    mockCallPost.mockResolvedValue({
      message: {
        net_total: 20000,
        discount_amount: 0,
        tax_rows: [{ description: 'VAT 18%', rate: 18, tax_amount: 3600 }],
        grand_total: 23600,
        rounded_total: 23600,
        item_taxes: [],
        efris_warnings: [],
      },
    });
    mockPosState.value = {
      searchQuery: '',
      setSearchQuery: vi.fn(),
      setShowVoluntaryClosing: vi.fn(),
      posProfile: {
        name: 'Main Till',
        currency: 'UGX',
        cashier: 'cashier@example.com',
        owner: 'cashier@example.com',
        enable_discount: 1,
        enable_payment_without_printing: 1,
      },
      currency: 'UGX',
      selectedOrderType: 'Dine In',
      selectedTable: 'T-1',
      selectedRoom: 'Main Hall',
      selectedCustomer: { id: 'Walk In', name: 'Walk In', phone: '' },
      selectedAggregator: null,
      activeServingPeriod: {
        period_name: 'Happy Hour',
        start_time: '16:00',
        end_time: '19:00',
      },
      activeTaxSummary: {
        tax_category: 'Dine-In VAT 18%',
        taxes_and_charges: 'VAT 18%',
        label: 'VAT 18%',
      },
      activeOrders: [
        {
          id: 'BURGER',
          item: 'BURGER',
          name: 'Beef Burger',
          price: 10000,
          quantity: 2,
          uniqueId: 'BURGER-default-no-addons',
        },
      ],
      removeFromOrder: vi.fn(),
      updateQuantity: vi.fn(),
      clearOrder: vi.fn(),
      setSelectedItem: vi.fn(),
      orderLoading: false,
      isOrderInteractionDisabled: () => false,
      isUpdatingOrder: false,
      resetOrderState: vi.fn(),
      paymentModes: ['Cash'],
      fetchPaymentModes: vi.fn(),
      orderId: null,
      originalSentItems: [],
      orderComment: '',
      setOrderComment: vi.fn(),
      guestLabel: '',
      setGuestLabel: vi.fn(),
      noOfPax: 2,
      setNoOfPax: vi.fn(),
      lastModifiedTime: null,
    };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('Header renders Daypart, Order-Type, and Tax status strip without vendor strings', () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>,
    );
    expect(screen.getByTestId('status-serving-period')).toHaveTextContent(/Happy Hour.*16:00–19:00/);
    expect(screen.getByTestId('status-order-type')).toHaveTextContent('Dine In');
    expect(screen.getByTestId('status-tax-badge')).toHaveTextContent('VAT 18%');
    expect(document.body.textContent || '').not.toMatch(VENDOR);
  });

  it('MenuCard renders promo strike-through price badge and disables out-of-daypart items with window label', async () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <MenuCard
        id="BEER"
        item="BEER"
        name="Craft Lager"
        price={10000}
        promoRate={7000}
        promoLabel="Happy Hour -30%"
        item_image={null}
        course="Drinks"
        currency="UGX"
        onClick={onClick}
      />,
    );
    expect(screen.getByText('Happy Hour -30%')).toBeInTheDocument();
    expect(screen.getByText('UGX 7000')).toBeInTheDocument();
    expect(screen.getByText('UGX 10000')).toBeInTheDocument();

    rerender(
      <MenuCard
        id="EGGS"
        item="EGGS"
        name="Breakfast Omelette"
        price={15000}
        unavailableDaypart
        daypartLabel="Available 06:00–11:00"
        item_image={null}
        course="Breakfast"
        currency="UGX"
        onClick={onClick}
      />,
    );
    expect(screen.getByText('Available 06:00–11:00')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Breakfast Omelette'));
    expect(onClick).not.toHaveBeenCalled();
  });

  it('OrderPanel debounces 150ms server preview_order_totals and renders Net, Tax, Total, and EFRIS alerts', async () => {
    mockCallPost.mockResolvedValueOnce({
      message: {
        net_total: 20000,
        discount_amount: 2000,
        tax_rows: [{ description: 'VAT 18%', rate: 18, tax_amount: 3240 }],
        grand_total: 21240,
        rounded_total: 21240,
        item_taxes: [],
        efris_warnings: [
          {
            code: 'EFRIS-ITEM-001',
            item_code: 'BURGER',
            message: 'EFRIS-ITEM-001: Item BURGER is missing EFRIS Commodity Code.',
          },
        ],
      },
    });

    render(<OrderPanel />);

    await waitFor(
      () => {
        expect(mockCallPost).toHaveBeenCalledWith(
          'ury.ury_pos.api.preview_order_totals',
          expect.objectContaining({
            pos_profile: 'Main Till',
            order_type: 'Dine In',
          }),
        );
      },
      { timeout: 1000 },
    );

    const footer = screen.getByTestId('order-tax-preview-footer');
    expect(footer).toHaveTextContent('UGX 20000');
    expect(footer).toHaveTextContent('VAT 18%');
    expect(footer).toHaveTextContent('UGX 3240');
    expect(footer).toHaveTextContent('UGX 21240');
    expect(screen.getByTestId('efris-readiness-alert')).toHaveTextContent('EFRIS-ITEM-001');
  });

  it('OrderPanel shows Offline estimate label when offline without hitting preview_order_totals', async () => {
    mockOutbox.value = { offline: true, replaying: false, queue: true, rows: [] };
    render(<OrderPanel />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 200));
    });
    expect(mockCallPost).not.toHaveBeenCalledWith('ury.ury_pos.api.preview_order_totals', expect.anything());
    expect(screen.getByTestId('offline-estimate-badge')).toHaveTextContent(
      /Offline estimate — verified on reconnect/i,
    );
  });

  it('PaymentDialog fetches server preview_order_totals for real orders and renders itemized taxes and EFRIS alert', async () => {
    mockCallPost.mockResolvedValueOnce({
      message: {
        net_total: 20000,
        discount_amount: 0,
        tax_rows: [{ description: 'VAT 18%', rate: 18, tax_amount: 3600 }],
        grand_total: 23600,
        rounded_total: 23600,
        item_taxes: [],
        efris_warnings: [
          {
            code: 'EFRIS-ITEM-001',
            item_code: 'BURGER',
            message: 'EFRIS-ITEM-001: Item BURGER is missing EFRIS Commodity Code.',
          },
        ],
      },
    });

    render(
      <PaymentDialog
        order={{ invoice_printed: 1, order_type: 'Dine In', currency: 'UGX' }}
        onClose={vi.fn()}
        grandTotal={23600}
        roundedTotal={23600}
        invoice="BILL-99"
        customer="Walk In"
        posProfile="Main Till"
        table="T-1"
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
      />,
    );

    await waitFor(
      () => {
        expect(mockCallPost).toHaveBeenCalledWith(
          'ury.ury_pos.api.preview_order_totals',
          expect.objectContaining({
            invoice_name: 'BILL-99',
            pos_profile: 'Main Till',
            order_type: 'Dine In',
          }),
        );
      },
      { timeout: 1000 },
    );

    expect(screen.getByTestId('payment-tax-breakdown')).toHaveTextContent('VAT 18%');
    expect(screen.getByTestId('payment-tax-breakdown')).toHaveTextContent('UGX 3600');
    expect(screen.getByTestId('payment-efris-alert')).toHaveTextContent('EFRIS-ITEM-001');
  });

  it('all 4 locales (en, fr, es, ar) include every new POS-PR-7 translation key cleanly', () => {
    const requiredKeys = [
      'cart.net_total',
      'cart.discount',
      'cart.tax',
      'cart.offline_estimate',
      'cart.tax_preview_loading',
      'cart.toggle_breakdown',
      'payment.net_total',
      'payment.tax',
      'payment.efris_warning_title',
      'header.tax_exempt',
      'menu.daypart_available',
    ];
    for (const [code, locale] of Object.entries({ en, fr, es, ar })) {
      const flat = flattenLocale(locale as Record<string, unknown>);
      for (const key of requiredKeys) {
        expect(flat[key], `${code} missing ${key}`).toBeTruthy();
        expect(flat[key], `${code}.${key} contains vendor string`).not.toMatch(VENDOR);
      }
    }
  });

  it('OrderPanel renders server-evaluated promo line price matching preview totals when no customer is selected', async () => {
    mockPosState.value = {
      ...mockPosState.value,
      selectedCustomer: null,
      selectedOrderType: 'Take Away',
      selectedTable: null,
      activeOrders: [
        {
          id: 'KH-BURGER-18',
          item: 'KH-BURGER-18',
          name: 'Classic Kampalan Burger',
          price: 20000,
          promo_rate: 16000,
          promo_label: 'Daypart Special -20%',
          quantity: 1,
          uniqueId: 'KH-BURGER-18-default',
        },
        {
          id: 'KH-NO-EFRIS-CODE',
          item: 'KH-NO-EFRIS-CODE',
          name: 'Unmapped Special Dish',
          price: 15000,
          quantity: 1,
          uniqueId: 'KH-NO-EFRIS-CODE-default',
        },
      ],
    };
    mockCallPost.mockResolvedValueOnce({
      message: {
        net_total: 31000,
        discount_amount: 0,
        tax_rows: [{ description: 'UG VAT 18% (18.0%)', rate: 18, tax_amount: 5580 }],
        grand_total: 36580,
        rounded_total: 36580,
        item_taxes: [
          { item_code: 'KH-BURGER-18', rate: 16000, net_amount: 16000 },
          { item_code: 'KH-NO-EFRIS-CODE', rate: 15000, net_amount: 15000 },
        ],
        efris_warnings: [],
      },
    });

    render(<OrderPanel />);

    await waitFor(
      () => {
        expect(screen.getByTestId('order-tax-preview-footer')).toHaveTextContent('UGX 36580');
      },
      { timeout: 1000 },
    );

    const panel = screen.getByTestId('order-panel');
    // Promo cart line must display the discounted 16000 price (matching MenuCard & preview net 31000)
    expect(panel).toHaveTextContent('UGX 16000');
    expect(screen.getByTestId('order-tax-preview-footer')).toHaveTextContent('UGX 31000');
  });

  it('PaymentDialog never displays a misleading header rate label when item tax templates override the effective rate', async () => {
    mockCallPost.mockResolvedValueOnce({
      message: {
        net_total: 31000,
        discount_amount: 0,
        tax_rows: [
          {
            description: 'UG Levy 10% (10.0%)',
            rate: 10,
            effective_rate: 18,
            item_rate_override: true,
            tax_amount: 5580,
            account_head: 'VAT - MFU',
          },
        ],
        grand_total: 36580,
        rounded_total: 36580,
        item_taxes: [
          { item_code: 'KH-BURGER-18', item_tax_template: 'Item VAT 18% - MFU', rate: 16000, net_amount: 16000 },
        ],
        efris_warnings: [],
      },
    });

    render(
      <PaymentDialog
        order={{ invoice_printed: 1, order_type: 'Delivery', currency: 'UGX' }}
        onClose={vi.fn()}
        grandTotal={36580}
        roundedTotal={36580}
        invoice="MFUA00030"
        customer="Walk In Guest"
        posProfile="POS Profile A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
      />,
    );

    await waitFor(
      () => {
        expect(screen.getByTestId('payment-tax-breakdown')).toHaveTextContent('UGX 5580');
      },
      { timeout: 1000 },
    );

    const breakdown = screen.getByTestId('payment-tax-breakdown');
    expect(breakdown).not.toHaveTextContent('10.0%');
    expect(breakdown).toHaveTextContent('18%');
  });

  it('MenuCard places promoBadge clear of AvailabilityActionMenu top-right controls with truncation', () => {
    render(
      <MenuCard
        id="KH-BURGER-18"
        item="KH-BURGER-18"
        name="Classic Kampalan Burger"
        price={20000}
        promoRate={16000}
        promoLabel="Daypart Special -20%"
        item_image={null}
        course="Main"
        currency="UGX"
        availabilityActions={{
          markSoldOut: vi.fn(),
          markAvailable: vi.fn(),
          setDailyLimit: vi.fn(),
        }}
      />,
    );
    const badge = screen.getByText('Daypart Special -20%');
    expect(badge).toHaveAttribute('title', 'Daypart Special -20%');
    expect(badge.className).toMatch(/\btruncate\b/);
    // Placed along the bottom of the image slot so top-right AvailabilityActionMenu ("...") never clips it
    expect(badge.className).toMatch(/\bbottom-/);
    expect(badge.className).not.toMatch(/\btop-1\b/);
  });

  it('OrderPanel renders a single authoritative Service Charge row from server preview without duplicating the client estimate', async () => {
    mockPosState.value = {
      ...mockPosState.value,
      posProfile: {
        ...(mockPosState.value.posProfile as Record<string, unknown>),
        custom_service_charge_rules: [
          {
            order_type: 'Dine In',
            enabled: 1,
            charge_mode: 'Percentage',
            rate: 10,
            fixed_amount: 0,
            min_amount_floor: 0,
            min_pax: 4,
            calculation_basis: 'Post-Discount Subtotal',
            taxable: 1,
            account_head: 'Service Charge Payable - KHH',
            description: 'Service Charge',
          },
        ],
      },
      selectedOrderType: 'Dine In',
      selectedTable: 'T-03',
      noOfPax: 4,
      activeOrders: [
        {
          id: 'KH-BURGER-18',
          item: 'KH-BURGER-18',
          name: 'Classic Beef Burger',
          price: 20000,
          promo_rate: 16000,
          promo_label: 'PRLE-0001 Happy Hour 20%',
          quantity: 1,
          uniqueId: 'KH-BURGER-18-default',
        },
      ],
    };
    mockCallPost.mockResolvedValueOnce({
      message: {
        net_total: 16000,
        discount_amount: 0,
        tax_rows: [
          {
            description: 'VAT 18%',
            rate: 18,
            tax_amount: 2880,
            account_head: 'VAT 18% Output - KHH',
            custom_pos_charge_kind: '',
          },
          {
            description: 'Service Charge',
            rate: 10,
            tax_amount: 1600,
            account_head: 'Service Charge Payable - KHH',
            custom_pos_charge_kind: 'Service Charge',
          },
          {
            description: 'VAT 18% on Service Charge',
            rate: 18,
            tax_amount: 288,
            account_head: 'VAT 18% Output - KHH',
            custom_pos_charge_kind: 'Service Charge Tax',
          },
        ],
        total_taxes_and_charges: 4768,
        grand_total: 20768,
        rounded_total: 20768,
        item_taxes: [{ item_code: 'KH-BURGER-18', rate: 16000, net_amount: 16000 }],
        efris_warnings: [],
      },
    });

    render(<OrderPanel />);

    await waitFor(
      () => {
        expect(screen.getByTestId('order-tax-preview-footer')).toHaveTextContent('UGX 20768');
      },
      { timeout: 1000 },
    );

    const footer = screen.getByTestId('order-tax-preview-footer');
    // Server authoritative lines sum strictly to 20,768 (16,000 + 2,880 + 1,600 + 288)
    expect(footer).toHaveTextContent('UGX 16000');
    expect(footer).toHaveTextContent('UGX 2880');
    expect(footer).toHaveTextContent('UGX 1600');
    expect(footer).toHaveTextContent('UGX 288');
    // Pre-discount client estimate (10% of 20,000 = UGX 2,000) MUST NOT appear anywhere in desktop or mobile summary
    expect(footer).not.toHaveTextContent('UGX 2000');
    expect(footer).not.toHaveTextContent(/4\+\s*guests/i);

    // Expanding mobile summary bar must also show single authoritative SC (UGX 1600), never duplicate UGX 2000
    await userEvent.click(screen.getByTestId('mobile-summary-toggle'));
    expect(footer).not.toHaveTextContent('UGX 2000');
    expect(footer).toHaveTextContent('UGX 1600');
  });

  it('OrderPanel forwards stable row identity (name & reservation_line_key) and renders #152 override/open/comp totals when editing an existing draft', async () => {
    mockPosState.value = {
      ...mockPosState.value,
      isUpdatingOrder: true,
      orderId: 'KHH00022',
      selectedOrderType: 'Take Away',
      selectedTable: null,
      noOfPax: 1,
      activeOrders: [
        {
          id: 'KH-BURGER-18',
          item: 'KH-BURGER-18',
          name: 'Classic Beef Burger',
          price: 12500,
          quantity: 1,
          uniqueId: 'row-ov',
          invoiceItemName: 'row-ov',
          reservationLineKey: 'rk-ov',
        },
        {
          id: 'KH-BURGER-SPICY-18',
          item: 'KH-BURGER-SPICY-18',
          name: 'Spicy Beef Burger',
          price: 16000,
          quantity: 1,
          uniqueId: 'row-prle',
          invoiceItemName: 'row-prle',
          reservationLineKey: 'rk-prle',
        },
        {
          id: 'KH-OPEN-ITEM',
          item: 'KH-OPEN-ITEM',
          name: 'Chef Special Open - Chef Special R4',
          price: 14000,
          quantity: 1,
          uniqueId: 'row-open',
          invoiceItemName: 'row-open',
          reservationLineKey: 'row-open',
        },
        {
          id: 'KH-WATER',
          item: 'KH-WATER',
          name: 'Still Mineral Water',
          price: 0,
          quantity: 1,
          uniqueId: 'row-comp',
          invoiceItemName: 'row-comp',
          reservationLineKey: 'rk-comp',
        },
        {
          id: 'KH-BURGER-18',
          item: 'KH-BURGER-18',
          name: 'Classic Beef Burger',
          price: 20000,
          quantity: 1,
          uniqueId: 'new-line-1',
          reservationLineKey: 'new-line-1',
        },
      ],
    };

    mockCallPost.mockResolvedValueOnce({
      message: {
        net_total: 58500,
        discount_amount: 0,
        tax_rows: [{ description: 'VAT 18%', rate: 18, tax_amount: 10530 }],
        total_taxes_and_charges: 10530,
        grand_total: 69030,
        rounded_total: 69030,
        item_taxes: [
          { item_code: 'KH-BURGER-18', rate: 12500, net_amount: 12500 },
          { item_code: 'KH-BURGER-SPICY-18', rate: 16000, net_amount: 16000 },
          { item_code: 'KH-OPEN-ITEM', rate: 14000, net_amount: 14000 },
          { item_code: 'KH-WATER', rate: 0, net_amount: 0 },
          { item_code: 'KH-BURGER-18', rate: 16000, net_amount: 16000 },
        ],
        efris_warnings: [],
      },
    });

    render(<OrderPanel />);

    await waitFor(
      () => {
        expect(mockCallPost).toHaveBeenCalledWith(
          'ury.ury_pos.api.preview_order_totals',
          expect.objectContaining({
            invoice_name: 'KHH00022',
            pos_profile: 'Main Till',
            order_type: 'Take Away',
          }),
        );
      },
      { timeout: 1000 },
    );

    const previewCall = mockCallPost.mock.calls.find(
      ([method]) => method === 'ury.ury_pos.api.preview_order_totals',
    );
    expect(previewCall).toBeTruthy();
    const sentItems = (previewCall![1] as { items: Array<Record<string, unknown>> }).items;
    expect(sentItems).toHaveLength(5);
    expect(sentItems[0]).toMatchObject({
      name: 'row-ov',
      reservation_line_key: 'rk-ov',
      item: 'KH-BURGER-18',
      qty: 1,
      rate: 12500,
    });
    expect(sentItems[2]).toMatchObject({
      name: 'row-open',
      reservation_line_key: 'row-open',
      item: 'KH-OPEN-ITEM',
      qty: 1,
      rate: 14000,
    });
    expect(sentItems[3]).toMatchObject({
      name: 'row-comp',
      reservation_line_key: 'rk-comp',
      item: 'KH-WATER',
      qty: 1,
      rate: 0,
    });
    expect(sentItems[4]).toMatchObject({
      reservation_line_key: 'new-line-1',
      item: 'KH-BURGER-18',
      qty: 1,
    });
    expect(sentItems[4]).not.toHaveProperty('name');

    await waitFor(
      () => {
        expect(screen.getByTestId('order-tax-preview-footer')).toHaveTextContent('UGX 69030');
      },
      { timeout: 1000 },
    );

    const panel = screen.getByTestId('order-panel');
    expect(panel).toHaveTextContent('UGX 12500');
    expect(panel).toHaveTextContent('UGX 14000');
    expect(panel).toHaveTextContent('UGX 0');
    const footer = screen.getByTestId('order-tax-preview-footer');
    expect(footer).toHaveTextContent('UGX 58500');
    expect(footer).toHaveTextContent('UGX 10530');
    expect(footer).toHaveTextContent('UGX 69030');
  });
});
