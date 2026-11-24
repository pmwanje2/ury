import fs from 'node:fs';
import path from 'node:path';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call, db, ownedSessionKey } from '@ury/core';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import { outstandingFields, type POSInvoice } from '../src/lib/invoice-api';
import type { PosProfileCombined } from '../src/lib/pos-profile-api';
import { printOrder } from '../src/lib/print';
import PaymentDialog from '../src/components/PaymentDialog';
import MergedBillPanel from '../src/components/MergedBillPanel';
import SplitGroupPanel from '../src/components/SplitGroupPanel';
import BillMergeDialog from '../src/components/BillMergeDialog';
import POSOpeningScreen from '../src/components/POSOpeningScreen';
import CaptainOrder from '../src/captain/pages/CaptainOrder';
import Footer from '../src/components/Footer';
import Header from '../src/components/Header';
import { PrinterWatchProvider } from '../src/components/PrinterWatch';
import Orders from '../src/pages/Orders';

vi.mock('../src/lib/print', () => ({ printOrder: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => { throw new Error('Offline'); } }));

let container: HTMLDivElement;
let root: Root;
const initialRoot = useRootStore.getState();
const initialPOS = usePOSStore.getState();

function makeProfile(overrides: Partial<PosProfileCombined> = {}): PosProfileCombined {
  return {
    name: 'Till A',
    owner: 'cashier@example.com',
    creation: '2026-11-24 08:00:00',
    modified: '2026-11-24 08:00:00',
    modified_by: 'cashier@example.com',
    docstatus: 0,
    idx: 0,
    company: 'Lakeside Hotel',
    customer: 'Walk-in Guest',
    country: 'Uganda',
    disabled: 0,
    warehouse: 'Stores',
    campaign: null,
    company_address: null,
    restaurant: 'Lakeside Grill',
    branch: 'Lakeside',
    currency: 'USD',
    role_allowed_for_billing: [],
    waiter: 'waiter@example.com',
    cashier: 'cashier@example.com',
    print_format: 'Receipt',
    qz_print: 0,
    qz_host: null,
    printer: null,
    print_type: 'socket',
    tableAttention: 0,
    paid_limit: 50,
    disable_rounded_total: 0,
    enable_discount: 1,
    multiple_cashier: 0,
    enable_payment_without_printing: 0,
    default_payment_mode: 'Cash',
    room_charge_modes: [],
    room_charge_unconfigured: false,
    ...overrides,
  };
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  document.cookie = 'user_id=cashier%40example.com';
  useRootStore.setState({
    user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['URY Cashier'] },
    ordersOwner: 'cashier@example.com',
    ordersBranch: 'Lakeside',
    orders: [],
    selectedOrder: null,
    selectedOrderItems: [],
    selectedOrderTaxes: [],
    selectedStatus: 'Return',
    orderLoading: false,
    selectedOrderLoading: false,
    error: null,
    selectedOrderError: null,
    pagination: { currentPage: 1, hasNextPage: false, itemsPerPage: 10 },
    fetchOrders: vi.fn(async () => {}),
    selectOrder: vi.fn(async () => true),
    setSelectedStatus: vi.fn(async () => {}),
    goToNextPage: vi.fn(async () => {}),
    goToPreviousPage: vi.fn(async () => {}),
    clearSelectedOrder: vi.fn(),
    orderSearchQuery: '',
  });
  usePOSStore.setState({
    paymentModes: ['Cash', 'Guest Folio'],
    fetchPaymentModes: vi.fn(async () => {}),
    posProfile: makeProfile(),
  });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ message: [] }) }));
  vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
    if (method === 'ury.ury_pos.api.getBranch') return { message: 'Lakeside' };
    if (method === 'frappe.client.get_list') return { message: [] };
    if (method === 'ury.ury_pos.api.getPosInvoiceItems') return { message: [[], []] };
    if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
    if (method === 'ury.ury_pos.api.get_split_group') return { message: { invoices: [], current: 'RET-1', group: null } };
    return { message: {} };
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  useRootStore.setState(initialRoot, true);
  usePOSStore.setState(initialPOS, true);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const button = (text: RegExp) =>
  Array.from(container.querySelectorAll('button')).find((b) => text.test(b.textContent || ''))!;

describe('POS-PR-2 R2 hermetic checks', () => {
  it('shows the exact room-charge setup message when Company has no guest ledger clearing mapped', async () => {
    usePOSStore.setState({
      posProfile: {
        ...usePOSStore.getState().posProfile!,
        room_charge_modes: [],
        room_charge_unconfigured: true,
      },
    });
    await act(async () =>
      root.render(
        <PaymentDialog
          order={{ invoice_printed: 1 }}
          onClose={vi.fn()}
          grandTotal={5.9}
          roundedTotal={5.9}
          invoice="CHK-1"
          customer="Walk-in Guest"
          posProfile="Till A"
          table={null}
          cashier="cashier@example.com"
          owner="cashier@example.com"
          fetchOrders={vi.fn(async () => {})}
          clearSelectedOrder={vi.fn()}
        />,
      ),
    );
    expect(container.textContent).toContain(
      "Room charge isn't set up for this company. Finance maps the guest ledger clearing account.",
    );
    expect(container.querySelector('#room-charge-reference')).toBeNull();
  });

  it('does not send an unapplied typed discount on Pay and charges the applied discount cleanly', async () => {
    const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { name: 'CHK-1', status: 'Submitted' } });
    await act(async () =>
      root.render(
        <PaymentDialog
          order={{ invoice_printed: 1 }}
          onClose={vi.fn()}
          grandTotal={35.4}
          roundedTotal={35.4}
          invoice="CHK-1"
          customer="Walk-in Guest"
          posProfile="Till A"
          table={null}
          cashier="cashier@example.com"
          owner="cashier@example.com"
          fetchOrders={vi.fn(async () => {})}
          clearSelectedOrder={vi.fn()}
        />,
      ),
    );
    const discountBox = container.querySelector<HTMLInputElement>('input[type="number"]:not([step])')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(discountBox, '10');
      discountBox.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => button(/^Pay /).click());
    expect(post).toHaveBeenCalledWith(
      'ury.ury.doctype.ury_order.ury_order.make_invoice',
      expect.objectContaining({
        additionalDiscount: null,
        payments: [{ mode_of_payment: 'Cash', amount: 35.4 }],
      }),
    );
  });

  it('renders the full return amount on the Return tab even when rounded_total is 0 and avoids Math.round on merged totals', async () => {
    const returnOrder: POSInvoice = {
      name: 'RET-0001',
      status: 'Return',
      invoice_printed: 0,
      grand_total: -32.45,
      rounded_total: 0,
      net_total: -27.5,
      total_taxes_and_charges: -4.95,
      restaurant_table: null,
      cashier: 'cashier@example.com',
      waiter: 'Waiter',
      customer: 'Walk-in Guest',
      mobile_number: '',
      posting_date: '2026-11-24',
      posting_time: '10:00:00',
      order_type: 'Dine In',
      custom_split_from: null,
      custom_merged_pos_invoice: null,
      custom_merged_total: 0,
    };
    const legacyMerged: POSInvoice = {
      ...returnOrder,
      name: 'LEG-0001',
      status: 'Draft',
      grand_total: 12.35,
      rounded_total: 12.35,
      custom_merged_pos_invoice: 'LEG-0002',
      custom_merged_total: 7.5,
    };
    useRootStore.setState({
      orders: [returnOrder, legacyMerged],
      selectedOrder: returnOrder,
      selectedStatus: 'Return',
    });
    await act(async () =>
      root.render(
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Orders />
        </MemoryRouter>,
      ),
    );
    const returnCard = container.querySelector('h3[title="RET-0001"]')!.closest('[class*="cursor-pointer"]')!;
    expect(returnCard.textContent).toContain('32.45');
    expect(returnCard.textContent).not.toContain('Merged bill');
    expect(returnCard.textContent).not.toContain('Split bill');

    const legacyCard = container.querySelector('h3[title="LEG-0001"]')!.closest('[class*="cursor-pointer"]')!;
    expect(legacyCard.textContent).toContain('19.85');
    expect(legacyCard.textContent).not.toContain('20.35');
  });

  it('MergedBillPanel displays the partner server total without Math.round', async () => {
    await act(async () =>
      root.render(
        <MergedBillPanel
          order={{
            name: 'LEG-0001',
            rounded_total: 12.35,
            custom_merged_pos_invoice: 'LEG-0002',
            custom_merged_total: 7.5,
          }}
        />,
      ),
    );
    expect(container.textContent).toContain('7.5');
    expect(container.textContent).not.toMatch(/₹\s*8\b|\$\s*8\b/);
  });

  it('hydrates existing draft tenders from getPosInvoiceItems and settles balance due keeping earlier tenders', async () => {
    vi.mocked(call.get).mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.api.getPosInvoiceItems') {
        return {
          message: [
            [{ name: 'ROW-1', item_name: 'Chicken Curry', qty: 1, rate: 15, amount: 15 }],
            [],
            [{ mode_of_payment: 'Cash', amount: 5.31, reference_no: 'RCV-01' }],
          ],
        };
      }
      if (method === 'ury.ury_pos.api.get_split_group') {
        return { message: { invoices: [], current: 'LKG-00061', group: null } };
      }
      return { message: {} };
    });
    const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { name: 'LKG-00061', status: 'Submitted' } });

    useRootStore.setState(initialRoot, true);
    useRootStore.setState({
      user: { name: 'cashier@example.com', full_name: 'Cashier', roles: ['URY Cashier'] },
      ordersOwner: 'cashier@example.com',
      ordersBranch: 'Lakeside',
    });
    usePOSStore.setState({
      paymentModes: ['Cash', 'Mobile Money'],
      fetchPaymentModes: vi.fn(async () => {}),
      currency: 'USD',
      posProfile: makeProfile({
        currency: 'USD',
        default_payment_mode: 'Mobile Money',
      }),
    });

    const draftOrder: POSInvoice = {
      name: 'LKG-00061',
      status: 'Draft',
      invoice_printed: 1,
      grand_total: 23.01,
      rounded_total: 23.01,
      net_total: 20,
      total_taxes_and_charges: 3.6,
      discount_amount: 0.59,
      additional_discount_percentage: 0,
      restaurant_table: 'T-101',
      cashier: 'cashier@example.com',
      waiter: 'Waiter',
      customer: 'Walk-in Guest',
      mobile_number: '',
      posting_date: '2026-11-24',
      posting_time: '10:00:00',
      order_type: 'Dine In',
    };

    await act(async () => {
      await useRootStore.getState().selectOrder(draftOrder);
    });

    const selected = useRootStore.getState().selectedOrder!;
    expect(selected.payments).toEqual([{ mode_of_payment: 'Cash', amount: 5.31, reference_no: 'RCV-01' }]);

    await act(async () =>
      root.render(
        <PaymentDialog
          order={selected}
          onClose={vi.fn()}
          grandTotal={23.01}
          roundedTotal={23.01}
          invoice="LKG-00061"
          customer="Walk-in Guest"
          posProfile="Till A"
          table="T-101"
          cashier="cashier@example.com"
          owner="cashier@example.com"
          fetchOrders={vi.fn(async () => {})}
          clearSelectedOrder={vi.fn()}
          discountPercentage={0}
          discountAmount={0.59}
        />,
      ),
    );

    expect(container.textContent).toContain('Already received');
    expect(container.textContent).toContain('RCV-01');
    expect(container.textContent).toContain('Balance due');
    const momoInput = container.querySelectorAll<HTMLInputElement>('input[type="number"][step="0.01"]')[1];
    expect(momoInput.value).toBe('17.7');

    await act(async () => button(/^Pay /).click());
    expect(post).toHaveBeenCalledWith(
      'ury.ury.doctype.ury_order.ury_order.make_invoice',
      expect.objectContaining({
        invoice: 'LKG-00061',
        payments: [
          { mode_of_payment: 'Cash', amount: 5.31, reference_no: 'RCV-01' },
          { mode_of_payment: 'Mobile Money', amount: 17.7 },
        ],
      }),
    );
  });

  it('formats multi-currency UGX till amounts in UGX even when site default currency is USD', async () => {
    vi.stubGlobal('frappe', { boot: { sysdefaults: { currency: 'USD' } } });
    usePOSStore.setState({
      paymentModes: ['Cash', 'Mobile Money'],
      fetchPaymentModes: vi.fn(async () => {}),
      currency: 'USD',
      posProfile: makeProfile({
        name: 'Kampala UGX Till',
        currency: 'UGX',
        default_payment_mode: 'Cash',
      }),
    });

    await act(async () =>
      root.render(
        <PaymentDialog
          order={{ invoice_printed: 1 }}
          onClose={vi.fn()}
          grandTotal={19057}
          roundedTotal={19100}
          invoice="KBG-00010"
          customer="Walk-in Guest"
          posProfile="Kampala UGX Till"
          table="K-101"
          cashier="cashier@example.com"
          owner="cashier@example.com"
          fetchOrders={vi.fn(async () => {})}
          clearSelectedOrder={vi.fn()}
        />,
      ),
    );

    expect(container.textContent).toContain('UGX 19,100');
    expect(container.textContent).not.toContain('USD 19,100');

    const cashInput = container.querySelectorAll<HTMLInputElement>('input[type="number"][step="0.01"]')[0];
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(cashInput, '20000');
      cashInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(container.textContent).toContain('UGX 900');
  });

  it('recovers cleanly after Step-2 submit failure so the cashier can switch methods and submit', async () => {
    vi.mocked(printOrder).mockClear();
    let submitAttempts = 0;
    const post = vi.spyOn(call, 'post').mockImplementation(async (method: string) => {
      if (method === 'ury.ury.doctype.ury_order.ury_order.submit_order_invoice') {
        submitAttempts += 1;
        if (submitAttempts === 1) throw new Error('Temporary submit failure');
      }
      return { message: { name: 'CHK-PRINT', status: 'Draft' } };
    });
    usePOSStore.setState({
      paymentModes: ['Cash', 'Mobile Money'],
      fetchPaymentModes: vi.fn(async () => {}),
      currency: 'USD',
      posProfile: makeProfile({
        currency: 'USD',
        default_payment_mode: 'Mobile Money',
        enable_payment_without_printing: 1,
      }),
    });

    await act(async () =>
      root.render(
        <PaymentDialog
          order={{ invoice_printed: 0 }}
          onClose={vi.fn()}
          grandTotal={5.9}
          roundedTotal={5.9}
          invoice="CHK-PRINT"
          customer="Walk-in Guest"
          posProfile="Till A"
          table="T-101"
          cashier="cashier@example.com"
          owner="cashier@example.com"
          fetchOrders={vi.fn(async () => {})}
          clearSelectedOrder={vi.fn()}
        />,
      ),
    );

    await act(async () => button(/^Pay /).click());
    expect(container.textContent).toMatch(/Temporary submit failure|Payment failed/i);
    expect(printOrder).not.toHaveBeenCalled();

    const inputs = container.querySelectorAll<HTMLInputElement>('input[type="number"][step="0.01"]');
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(inputs[1], '');
      inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(inputs[0], '5.9');
      inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => button(/^Pay /).click());

    expect(post).toHaveBeenCalledWith(
      'ury.ury.doctype.ury_order.ury_order.make_invoice',
      expect.objectContaining({
        invoice: 'CHK-PRINT',
        payments: [{ mode_of_payment: 'Cash', amount: 5.9 }],
        submit_invoice: 0,
      }),
    );
    expect(post).toHaveBeenCalledWith(
      'ury.ury.doctype.ury_order.ury_order.submit_order_invoice',
      { invoice: 'CHK-PRINT' },
    );
  });

  it('Orders displays the order own currency (UGX) on cards, items, taxes, and sticky totals on a USD-default site', async () => {
    vi.stubGlobal('frappe', { boot: { sysdefaults: { currency: 'USD' } } });
    expect(outstandingFields).toContain('currency');

    const ugxOrder: POSInvoice = {
      name: 'KBG-00003',
      status: 'Draft',
      currency: 'UGX',
      invoice_printed: 0,
      grand_total: 19057,
      rounded_total: 19100,
      net_total: 16150,
      total_taxes_and_charges: 2907,
      restaurant_table: 'K-101',
      cashier: 'cashier@example.com',
      waiter: 'cashier@example.com',
      customer: 'Walk-in Guest',
      mobile_number: '',
      posting_date: '2026-11-24',
      posting_time: '11:54:00',
      order_type: 'Dine In',
    };

    usePOSStore.setState({
      currency: 'USD',
      posProfile: makeProfile({ name: 'Kampala UGX Till', currency: 'UGX', branch: 'Kampala' }),
    });
    useRootStore.setState({
      selectedStatus: 'Outstanding',
      orders: [ugxOrder],
      selectedOrder: ugxOrder,
      selectedOrderItems: [
        { name: 'row-1', item_name: 'Grilled Tilapia', qty: 1, rate: 16150, price_list_rate: 16150, amount: 16150 },
      ],
      selectedOrderTaxes: [{ description: 'VAT 18%', rate: 2907 }],
    });

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={['/orders']}>
          <Orders />
        </MemoryRouter>,
      ),
    );

    const text = container.textContent || '';
    expect(text).toContain('UGX 19,100');
    expect(text).toContain('UGX 16,150');
    expect(text).toContain('UGX 2,907');
    expect(text).not.toContain('USD 19,100');
    expect(text).not.toContain('USD 16,150');
    expect(text).not.toContain('USD 2,907');
  });

  it('Footer and Header use phone-safe flex sizing so /orders does not overflow at 390px', async () => {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={['/orders']}>
          <PrinterWatchProvider>
            <Header />
            <Footer />
          </PrinterWatchProvider>
        </MemoryRouter>,
      ),
    );

    const footerLinks = Array.from(container.querySelectorAll('nav a'));
    expect(footerLinks.length).toBeGreaterThanOrEqual(6);
    for (const link of footerLinks) {
      const classes = link.className.split(/\s+/);
      expect(classes).not.toContain('min-w-[4.5rem]');
      expect(classes).toContain('min-w-0');
    }

    const searchInput = container.querySelector('header input');
    expect(searchInput).toBeTruthy();
    const searchWrapperClasses = searchInput!.parentElement!.className.split(/\s+/);
    expect(searchWrapperClasses).toContain('min-w-0');
    expect(searchWrapperClasses).not.toContain('mx-8');
  });

  it('SplitGroupPanel, BillMergeDialog, and MergedBillPanel format UGX (never USD) on a USD-default site', async () => {
    vi.stubGlobal('frappe', { boot: { sysdefaults: { currency: 'USD' } } });
    usePOSStore.setState({
      currency: 'USD',
      posProfile: makeProfile({ name: 'Kampala UGX Till', currency: 'UGX', branch: 'Kampala' }),
    });

    const profileKey = ownedSessionKey('posProfile');
    if (profileKey) {
      sessionStorage.setItem(profileKey, JSON.stringify({ branch: 'Kampala', currency: 'UGX' }));
    }

    // 1. SplitGroupPanel
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.api.get_split_group') {
        return {
          message: {
            group: 'SG-UGX',
            current: 'KBG-00010',
            invoices: [
              {
                name: 'KBG-00010',
                currency: 'UGX',
                status: 'Draft',
                docstatus: 0,
                invoice_printed: 0,
                grand_total: 19057,
                rounded_total: 19100,
                net_total: 16150,
                total_taxes_and_charges: 2907,
                restaurant_table: 'K-101',
                cashier: 'cashier@example.com',
                waiter: 'cashier@example.com',
                customer: 'Walk-in Guest',
                mobile_number: '',
                posting_date: '2026-11-24',
                posting_time: '12:00:00',
                order_type: 'Dine In',
                split_index: 1,
                split_total: 2,
                is_original: true,
              },
              {
                name: 'KBG-00011',
                currency: 'UGX',
                status: 'Draft',
                docstatus: 0,
                invoice_printed: 0,
                grand_total: 12000,
                rounded_total: 12000,
                net_total: 10169,
                total_taxes_and_charges: 1831,
                restaurant_table: 'K-101',
                cashier: 'cashier@example.com',
                waiter: 'cashier@example.com',
                customer: 'Walk-in Guest',
                mobile_number: '',
                posting_date: '2026-11-24',
                posting_time: '12:01:00',
                order_type: 'Dine In',
                split_index: 2,
                split_total: 2,
                is_original: false,
              },
            ],
          },
        };
      }
      if (method === 'ury.ury_pos.api.getPosInvoiceItems') {
        return {
          message: [
            [{ name: 'row-m1', item_name: 'Grilled Tilapia', qty: 1, rate: 16150, price_list_rate: 16150, amount: 16150 }],
            [],
            [],
          ],
        };
      }
      return { message: {} };
    });

    await act(async () =>
      root.render(<SplitGroupPanel invoiceName="KBG-00010" onOpenInvoice={vi.fn()} />),
    );
    const splitText = container.textContent || '';
    expect(splitText).toContain('UGX 19,100');
    expect(splitText).toContain('UGX 12,000');
    expect(splitText).not.toContain('USD');

    // 2. MergedBillPanel
    await act(async () =>
      root.render(
        <MergedBillPanel
          order={{
            name: 'KBG-00010',
            currency: 'UGX',
            custom_merged_pos_invoice: 'KBG-00012',
            custom_merged_total: 16150,
            rounded_total: 19100,
          }}
        />,
      ),
    );
    const mergedText = container.textContent || '';
    expect(mergedText).toContain('UGX 16,150');
    expect(mergedText).toContain('UGX 35,250');
    expect(mergedText).not.toContain('USD');

    // 3. BillMergeDialog (+ MERGE_CANDIDATE_FIELDS includes currency)
    const getDocListSpy = vi.spyOn(db, 'getDocList').mockImplementation(async (_dt, args) => {
      const fields = (args as { fields?: readonly string[] } | undefined)?.fields ?? [];
      if (fields.includes('custom_merged_pos_invoice')) {
        return [];
      }
      return [
        {
          name: 'KBG-00015',
          currency: 'UGX',
          customer: 'Walk-in Guest',
          customer_name: 'Walk-in Guest',
          grand_total: 25000,
          rounded_total: 25000,
          posting_date: '2026-11-24',
          posting_time: '12:05:00',
          order_type: 'Dine In',
          restaurant_table: 'K-102',
          custom_merged_tables: null,
          status: 'Draft',
          invoice_printed: 0,
        },
      ];
    });

    await act(async () =>
      root.render(
        <BillMergeDialog
          open
          onOpenChange={vi.fn()}
          invoiceName="KBG-00010"
          onConfirm={vi.fn(async () => {})}
        />,
      ),
    );
    const candidateCall = getDocListSpy.mock.calls.find(([, args]) => {
      const fields = (args as { fields?: readonly string[] } | undefined)?.fields ?? [];
      return fields.includes('rounded_total');
    });
    expect(candidateCall).toBeTruthy();
    expect(((candidateCall![1] as { fields?: readonly string[] }).fields) ?? []).toContain('currency');

    const dialogText = document.body.textContent || '';
    expect(dialogText).toContain('UGX 25,000');
    expect(dialogText).not.toContain('USD 25,000');
  });

  it('guard: every formatCurrency call in pos/src passes an explicit currency argument', () => {
    const srcRoot = path.resolve(__dirname, '../src');
    const offenders: string[] = [];

    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === '__tests__') continue;
          walk(full);
          continue;
        }
        if (!/\.(ts|tsx)$/.test(entry.name)) continue;
        if (/\.(test|spec)\.(ts|tsx)$/.test(entry.name)) continue;

        const source = fs.readFileSync(full, 'utf8');
        const re = /\bformatCurrency\s*\(/g;
        let match: RegExpExecArray | null;
        while ((match = re.exec(source)) !== null) {
          let i = match.index + match[0].length;
          let depth = 1;
          let hasTopLevelComma = false;
          let inSingle = false;
          let inDouble = false;
          let inTemplate = false;
          let escaped = false;
          while (i < source.length && depth > 0) {
            const ch = source[i];
            if (escaped) {
              escaped = false;
              i += 1;
              continue;
            }
            if (inSingle) {
              if (ch === '\\') escaped = true;
              else if (ch === "'") inSingle = false;
            } else if (inDouble) {
              if (ch === '\\') escaped = true;
              else if (ch === '"') inDouble = false;
            } else if (inTemplate) {
              if (ch === '\\') escaped = true;
              else if (ch === '`') inTemplate = false;
            } else if (ch === "'") {
              inSingle = true;
            } else if (ch === '"') {
              inDouble = true;
            } else if (ch === '`') {
              inTemplate = true;
            } else if (ch === '(' || ch === '[' || ch === '{') {
              depth += 1;
            } else if (ch === ')' || ch === ']' || ch === '}') {
              depth -= 1;
            } else if (ch === ',' && depth === 1) {
              hasTopLevelComma = true;
            }
            i += 1;
          }
          if (!hasTopLevelComma) {
            const line = source.slice(0, match.index).split('\n').length;
            const rel = path.relative(path.resolve(__dirname, '..'), full);
            offenders.push(`${rel}:${line}`);
          }
        }
      }
    };

    walk(srcRoot);
    expect(offenders).toEqual([]);
  });

  it('CaptainOrder passes tillCurrency to BillSplitDialog so UGX checks never show USD', async () => {
    vi.stubGlobal('frappe', { boot: { sysdefaults: { currency: 'USD' } } });
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury.doctype.ury_order.ury_order.get_table_order_context') {
        return {
          message: {
            table: { name: 'K-101', restaurant_room: 'Main', is_take_away: 0 },
            order: {
              name: 'KBG-00016',
              customer: 'Walk-in Guest',
              customer_name: 'Walk-in Guest',
              mobile_number: '',
              custom_split_group: null,
            },
            assignment: null,
            permissions: {
              view: true,
              modify: true,
              reduce_items: true,
              remove_items: true,
              reprint_kot: false,
              transfer_table: false,
              transfer_captain: false,
              print_bill: false,
              split_bill: true,
            },
          },
        };
      }
      if (method === 'ury.ury_pos.api.getRestaurantMenu') {
        return { message: { items: [], modified_time: '', name: 'Menu' } };
      }
      return { message: {} };
    });

    usePOSStore.setState({
      currency: 'USD',
      posProfile: makeProfile({ name: 'Kampala UGX Till', currency: 'UGX', branch: 'Kampala Branch' }),
      selectedOrderType: 'Dine In',
      orderId: 'KBG-00016',
      isUpdatingOrder: true,
      orderLoading: true,
      activeOrders: [
        {
          id: 'TILAPIA',
          uniqueId: 'row-1',
          invoiceItemName: 'row-1',
          name: 'Grilled Tilapia',
          price: 25000,
          quantity: 1,
          item: 'TILAPIA',
        },
        {
          id: 'SODA',
          uniqueId: 'row-2',
          invoiceItemName: 'row-2',
          name: 'Passion Juice',
          price: 5000,
          quantity: 1,
          item: 'SODA',
        },
      ],
      setSelectedTable: vi.fn(() => {
        Promise.resolve().then(() => usePOSStore.setState({ orderLoading: false }));
      }),
      loadTableOrder: vi.fn(async () => {}),
      clearTableOrder: vi.fn(),
      isOrderInteractionDisabled: () => false,
    });

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={['/order/K-101']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Routes>
            <Route path="/order/:table" element={<CaptainOrder />} />
          </Routes>
        </MemoryRouter>,
      ),
    );

    const actionsBtn = container.querySelector<HTMLButtonElement>('button[aria-expanded]');
    expect(actionsBtn).toBeTruthy();
    await act(async () => actionsBtn!.click());

    const splitBtn = Array.from(container.querySelectorAll('button')).find((b) =>
      /Split bill/i.test(b.textContent || ''),
    );
    expect(splitBtn).toBeTruthy();
    await act(async () => splitBtn!.click());

    const dialogText = document.body.textContent || '';
    expect(dialogText).toContain('UGX 25,000');
    expect(dialogText).toContain('UGX 5,000');
    expect(dialogText).toContain('UGX 30,000');
    expect(dialogText).toContain('UGX 0');
    expect(dialogText).not.toContain('USD 25,000');
    expect(dialogText).not.toContain('USD 5,000');
    expect(dialogText).not.toContain('USD 30,000');
    expect(dialogText).not.toContain('USD 0');
  });

  it('guard: every BillSplitDialog, BillMergeDialog, SplitGroupPanel, and MergedBillPanel JSX call passes currency', () => {
    const srcRoot = path.resolve(__dirname, '../src');
    const offenders: string[] = [];
    const targets = new Set(['BillSplitDialog', 'BillMergeDialog', 'SplitGroupPanel', 'MergedBillPanel']);

    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === '__tests__') continue;
          walk(full);
          continue;
        }
        if (!/\.tsx$/.test(entry.name) || /\.(test|spec)\.tsx$/.test(entry.name)) continue;

        const source = fs.readFileSync(full, 'utf8');
        const re = /<(BillSplitDialog|BillMergeDialog|SplitGroupPanel|MergedBillPanel)\b/g;
        let match: RegExpExecArray | null;
        while ((match = re.exec(source)) !== null) {
          if (!targets.has(match[1])) continue;
          // Scan JSX opening tag attributes respecting nested braces and quotes.
          let i = match.index + match[0].length;
          let braceDepth = 0;
          let inSingle = false;
          let inDouble = false;
          while (i < source.length) {
            const ch = source[i];
            if (inSingle) {
              if (ch === '\\') { i += 2; continue; }
              if (ch === "'") inSingle = false;
            } else if (inDouble) {
              if (ch === '\\') { i += 2; continue; }
              if (ch === '"') inDouble = false;
            } else if (ch === "'") {
              inSingle = true;
            } else if (ch === '"') {
              inDouble = true;
            } else if (ch === '{') {
              braceDepth += 1;
            } else if (ch === '}') {
              braceDepth = Math.max(0, braceDepth - 1);
            } else if (ch === '>' && braceDepth === 0) {
              break;
            }
            i += 1;
          }
          const tagText = source.slice(match.index, i + 1);
          if (!/\bcurrency\s*=/.test(tagText)) {
            const line = source.slice(0, match.index).split('\n').length;
            const rel = path.relative(path.resolve(__dirname, '..'), full);
            offenders.push(`${rel}:${line} <${match[1]}>`);
          }
        }
      }
    };

    walk(srcRoot);
    expect(offenders).toEqual([]);
  });

  it('POSOpeningScreen formats UGX 0 and enables Open POS Session on a USD-default multi-company site', async () => {
    vi.stubGlobal('frappe', { boot: { sysdefaults: { currency: 'USD' } } });
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'ury.ury_pos.api.get_pos_opening_screen_data') {
        return {
          message: {
            user: 'cashier_ugx@example.com',
            user_full_name: 'Kampala Cashier',
            company: 'Kampala Grill',
            company_currency: 'UGX',
            currency_symbol: 'USh',
            allowed_profiles: [{ name: 'Kampala UGX Till', label: 'Kampala UGX Till' }],
            selected_profile: 'Kampala UGX Till',
            branch: 'Kampala Branch',
            restaurant: 'Kampala Grill',
            payment_modes: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
            daily_close_pending: false,
            multi_cashier: { enabled: false, main_cashier_configured: false, main_cashier_open: false },
            permissions: { create: true, submit: true },
            open_entries: [],
            occupied_entry: null,
          },
        };
      }
      return { message: {} };
    });

    await act(async () => root.render(<POSOpeningScreen />));
    const text = container.textContent || '';
    expect(text).toContain('Kampala Grill');
    expect(text).toContain('Kampala UGX Till');
    expect(text).toContain('UGX 0');
    expect(text).not.toContain('USD 0');

    const openBtn = button(/Open POS Session/i);
    expect(openBtn).toBeTruthy();
    expect(openBtn.disabled).toBe(false);
  });
});
