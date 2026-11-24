import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import type { POSInvoice } from '../src/lib/invoice-api';
import type { PosProfileCombined } from '../src/lib/pos-profile-api';
import { printOrder } from '../src/lib/print';
import PaymentDialog from '../src/components/PaymentDialog';
import MergedBillPanel from '../src/components/MergedBillPanel';
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

  it('recovers cleanly after Step-2 print failure so the cashier can switch methods and submit', async () => {
    vi.mocked(printOrder)
      .mockRejectedValueOnce(new Error('Receipt printer offline'))
      .mockResolvedValueOnce('qz');
    const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { name: 'CHK-PRINT', status: 'Draft' } });
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
    expect(container.textContent).toContain('Payment was not processed');

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
});
