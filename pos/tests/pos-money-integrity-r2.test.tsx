import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../src/i18n';
import { useRootStore } from '../src/store/root-store';
import { usePOSStore } from '../src/store/pos-store';
import PaymentDialog from '../src/components/PaymentDialog';
import MergedBillPanel from '../src/components/MergedBillPanel';
import Orders from '../src/pages/Orders';

vi.mock('../src/lib/print', () => ({ printOrder: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => { throw new Error('Offline'); } }));

let container: HTMLDivElement;
let root: Root;
const initialRoot = useRootStore.getState();
const initialPOS = usePOSStore.getState();

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
    pagination: { currentPage: 1, hasNextPage: false, hasPreviousPage: false },
    fetchOrders: vi.fn(async () => {}),
    selectOrder: vi.fn(async () => true),
    setSelectedStatus: vi.fn(),
    goToNextPage: vi.fn(),
    goToPreviousPage: vi.fn(),
    clearSelectedOrder: vi.fn(),
    orderSearchQuery: '',
  } as any);
  usePOSStore.setState({
    paymentModes: ['Cash', 'Guest Folio'],
    fetchPaymentModes: vi.fn(async () => {}),
    posProfile: {
      name: 'Till A',
      default_payment_mode: 'Cash',
      enable_discount: 1,
      enable_payment_without_printing: 0,
      room_charge_modes: [],
      room_charge_unconfigured: false,
    } as any,
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
    const returnOrder = {
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
    const legacyMerged = {
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
    } as any);
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
});
