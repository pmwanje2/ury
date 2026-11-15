import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import { getSplitGroup, mapSplitGroupInvoiceToPOSInvoice, type SplitGroupInvoice } from '../src/lib/invoice-api';
import SplitGroupPanel from '../src/components/SplitGroupPanel';
import CaptainOrder from '../src/captain/pages/CaptainOrder';

// M03 carry-through: a check named "Susan" or "David" stays named wherever a
// waiter or cashier meets it - the split list, the order pad - and the name
// can be cleared again. It is a short label, never a Customer.

const state = vi.hoisted(() => ({
  activeOrders: [{ id: 'TEA', uniqueId: 'ROW-1', name: 'Tea', price: 100, quantity: 1 }],
  posProfile: { name: 'POS-1', cashier: 'Cashier', owner: 'Owner', customer: 'Walk In' },
  selectedCustomer: null, selectedRoom: 'Dining', paymentModes: ['Cash'],
  orderComment: '', guestLabel: '', noOfPax: 2, lastModifiedTime: '2026-10-22 10:00:00',
  orderId: null as string | null, isUpdatingOrder: false,
  user: { name: 'waiter@example.com' },
  clearTableOrder: vi.fn(), setNoOfPax: vi.fn(), setSelectedItem: vi.fn(), removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(), updateItemComment: vi.fn(), setGuestLabel: vi.fn(),
}));
const syncOrder = vi.hoisted(() => vi.fn());

vi.mock('../src/lib/invoice-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/invoice-api')>();
  return { ...actual, getSplitGroup: vi.fn() };
});
vi.mock('../src/store/pos-store', () => ({ usePOSStore: () => ({ ...state, isOrderInteractionDisabled: () => false }) }));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: (value: { user: typeof state.user }) => unknown) => select({ user: state.user }) }));
vi.mock('../src/captain/hooks/useTableOrderContext', () => ({
  useTableOrderContext: () => ({
    context: { order: null, assignment: null },
    permissions: { view: true, modify: true, reduce_items: false, remove_items: false, reprint_kot: false,
      transfer_table: false, transfer_captain: false, print_bill: false, split_bill: false },
    isContextLoading: false, contextError: null, isOrderReady: true,
    alreadyOrderedLines: [], newOrChangedLines: [], reductionPendingLines: [],
    refetchContext: vi.fn(), reloadOrder: vi.fn(),
  }),
}));
vi.mock('../src/offline/use-outbox', () => ({ useOutbox: () => ({ offline: false, rows: [], replaying: false, queue: null }) }));
vi.mock('../src/lib/order-api', () => ({
  captainTransfer: vi.fn(), getCaptainTransferCandidates: vi.fn(), reprintKot: vi.fn(),
  splitBill: vi.fn(), syncOrder, tableTransfer: vi.fn(),
}));
vi.mock('../src/lib/print', () => ({ printOrder: vi.fn() }));
vi.mock('../src/lib/table-api', () => ({ getVacantTablesForBranch: vi.fn() }));
vi.mock('../src/lib/floor-session', () => ({ floorSignOut: vi.fn(), isFloorPadActive: vi.fn(() => false), isStaySignedIn: vi.fn(() => false) }));
vi.mock('../src/captain/components/CaptainMenu', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainOrderLine', () => ({ default: () => null }));
vi.mock('../src/captain/components/CaptainActionsMenu', () => ({ default: () => null }));
vi.mock('../src/components/ProductDialog', () => ({ default: () => null }));
vi.mock('../src/components/CommentDialog', () => ({ default: () => null }));
vi.mock('../src/components/TableTransferDialog', () => ({ default: () => null }));
vi.mock('../src/components/CaptainTransferDialog', () => ({ default: () => null }));
vi.mock('../src/components/CustomerSelect', () => ({ CustomerSelect: () => null }));
vi.mock('../src/components/HoldFireControls', () => ({ HoldFireControls: () => null }));
vi.mock('../src/components/BillSplitDialog', () => ({ default: () => null }));

let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  vi.clearAllMocks();
  Object.assign(state, { guestLabel: '', isUpdatingOrder: false, orderId: null });
  syncOrder.mockResolvedValue({ message: { name: 'INV-1' } });
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

const check = (name: string, label: string | null, index: number): SplitGroupInvoice => ({
  name, invoice_printed: 1, grand_total: 100, restaurant_table: 'T-4', cashier: 'c', waiter: 'w',
  net_total: 100, posting_time: '10:00:00', total_taxes_and_charges: 0, customer: 'Walk In',
  customer_name: 'Walk In', status: 'Draft', mobile_number: '', posting_date: '2026-10-22',
  rounded_total: 100, order_type: 'Dine In', custom_split_group: 'SG-1',
  custom_split_from: index === 1 ? null : 'INV-A', split_index: index, split_total: 2,
  is_original: index === 1, docstatus: 0, custom_guest_label: label,
} as SplitGroupInvoice);

describe('split checks keep their guest label', () => {
  it('mapSplitGroupInvoiceToPOSInvoice keeps custom_guest_label', () => {
    expect(mapSplitGroupInvoiceToPOSInvoice(check('INV-B', 'David', 2)).custom_guest_label).toBe('David');
  });

  it('SplitGroupPanel tells two walk-in checks apart by their label', async () => {
    vi.mocked(getSplitGroup).mockResolvedValue({
      invoices: [check('INV-A', 'Susan', 1), check('INV-B', 'David', 2)], current: 'INV-A', group: 'SG-1',
    });
    await act(async () => root.render(<SplitGroupPanel invoiceName="INV-A" onOpenInvoice={vi.fn()} />));
    const rows = [...host.querySelectorAll('[role="button"]')].map((row) => row.textContent || '');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toContain('Susan');
    expect(rows[1]).toContain('David');
    // the label leads, ahead of the shared walk-in customer
    expect(rows[1].indexOf('David')).toBeLessThan(rows[1].indexOf('Walk In'));
  });

  it('SplitGroupPanel without labels still shows the customer', async () => {
    vi.mocked(getSplitGroup).mockResolvedValue({
      invoices: [check('INV-A', null, 1), check('INV-B', '', 2)], current: 'INV-A', group: 'SG-1',
    });
    await act(async () => root.render(<SplitGroupPanel invoiceName="INV-A" onOpenInvoice={vi.fn()} />));
    const rows = [...host.querySelectorAll('[role="button"]')].map((row) => row.textContent || '');
    expect(rows.every((row) => row.includes('Walk In'))).toBe(true);
  });
});

describe('Captain order pad guest label', () => {
  const render = () => act(async () => root.render(
    <MemoryRouter initialEntries={['/order/table/T-1']}>
      <Routes><Route path="/order/table/:table" element={<CaptainOrder />} /></Routes>
    </MemoryRouter>,
  ));
  const box = () => host.querySelector<HTMLInputElement>('input[name="guest_label"]');
  const send = async () => {
    const button = [...host.querySelectorAll('button')].find((b) => /Send Order|Update Order/.test(b.textContent || ''));
    expect(button).toBeDefined();
    await act(async () => button!.click());
  };

  it('has a 40-character guest label box bound to the store', async () => {
    await render();
    expect(box()).not.toBeNull();
    expect(box()!.maxLength).toBe(40);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(box()!, 'Susan');
      box()!.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(state.setGuestLabel).toHaveBeenCalledWith('Susan');
  });

  it('clearing the label on an existing order sends it blank so the server clears it', async () => {
    Object.assign(state, { guestLabel: '', isUpdatingOrder: true, orderId: 'INV-1' });
    await render();
    await send();
    expect(syncOrder).toHaveBeenCalledWith(expect.objectContaining({ invoice: 'INV-1', guest_label: '' }));
  });

  it('a new order without a label sends none', async () => {
    await render();
    await send();
    expect(syncOrder.mock.calls[0][0]).not.toHaveProperty('guest_label');
  });
});
