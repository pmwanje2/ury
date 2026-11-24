import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { showToast } from '@ury/ui';
import { initI18n } from '../src/i18n';
import Orders from '../src/pages/Orders';

const printMocks = vi.hoisted(() => ({
  printOrder: vi.fn().mockResolvedValue('socket'),
  printBillCopy: vi.fn().mockResolvedValue({ status: 'COMPLETED' }),
}));

vi.mock('../src/lib/print', () => ({
  printOrder: printMocks.printOrder,
  printBillCopy: printMocks.printBillCopy,
}));

const orderState = vi.hoisted(() => ({
  fetchOrders: vi.fn(),
  selectOrder: vi.fn(),
  order: {
    name: 'POS-INV-SETTLED-1',
    status: 'Paid' as const,
    invoice_printed: 1,
    currency: 'UGX',
    grand_total: 45000,
    rounded_total: 45000,
    net_total: 45000,
    total_taxes_and_charges: 0,
    restaurant_table: 'T1',
    custom_merged_tables: 'T1, T2',
    custom_merged_pos_invoice: null,
    cashier: 'cashier@example.com',
    waiter: 'waiter@example.com',
    customer: 'Walk In',
    mobile_number: '',
    posting_date: '2026-11-24',
    posting_time: '12:00:00',
    order_type: 'Dine In' as const,
    age_minutes: 10,
  },
}));

vi.mock('../src/store/root-store', () => ({
  useRootStore: () => ({
    ordersOwner: 'cashier@example.com',
    orders: [orderState.order],
    selectedOrder: orderState.order,
    selectedOrderItems: [
      { name: 'ROW-1', item_code: 'BURGER', item_name: 'Burger', qty: 2, rate: 15000, price_list_rate: 15000, amount: 30000 },
      { name: 'ROW-2', item_code: 'JUICE', item_name: 'Fresh Juice', qty: 1, rate: 15000, price_list_rate: 15000, amount: 15000 },
    ],
    selectedOrderTaxes: [],
    selectedStatus: 'Paid',
    user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['Cashier'] },
    orderLoading: false,
    selectedOrderLoading: false,
    error: null,
    selectedOrderError: null,
    pagination: { currentPage: 1, hasNextPage: false, hasPreviousPage: false },
    fetchOrders: orderState.fetchOrders,
    selectOrder: orderState.selectOrder,
    setSelectedStatus: vi.fn(),
    goToNextPage: vi.fn(),
    goToPreviousPage: vi.fn(),
    clearSelectedOrder: vi.fn(),
    orderSearchQuery: '',
  }),
}));

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: {
      name: 'Main Till',
      currency: 'UGX',
      view_all_status: 1,
      paid_limit: 10,
      print_type: 'browser',
      print_format: 'Merged POS Invoice Format',
    },
    paymentModes: ['Cash', 'Card'],
    fetchPaymentModes: vi.fn(),
  }),
}));

vi.mock('../src/components/OrderStatusSidebar', () => ({ default: () => null }));
vi.mock('../src/components/OrderActionsMenu', () => ({ default: () => null }));
vi.mock('../src/components/SplitGroupPanel', () => ({ default: () => null }));
vi.mock('../src/components/MergedBillPanel', () => ({ default: () => null }));

beforeEach(async () => {
  document.cookie = 'user_id=cashier%40example.com; Path=/';
  await initI18n('en');
  printMocks.printOrder.mockClear();
  orderState.fetchOrders.mockClear();
  vi.spyOn(showToast, 'error').mockImplementation(() => '' as never);
  vi.spyOn(showToast, 'success').mockImplementation(() => '' as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POS Orders screen — settled single & Add Check return/reopen and receipt printing (G8)', () => {
  it('exposes Return / Refund and Reopen / Adjust on settled Add Check bills and prints the return receipt', async () => {
    vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
      if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
      if (method === 'ury.ury_pos.void_return_reopen.get_returnable_check') {
        return {
          message: {
            invoice: 'POS-INV-SETTLED-1',
            pos_profile: 'Main Till',
            branch: 'Branch A',
            customer: 'Walk In',
            currency: 'UGX',
            precision: 0,
            grand_total: 45000,
            discount_amount: 0,
            total_taxes_and_charges: 0,
            net_paid_amount: 45000,
            already_refunded_amount: 0,
            remaining_refundable_amount: 45000,
            original_payments: [{ mode_of_payment: 'Cash', amount: 45000 }],
            available_modes: ['Cash', 'Card'],
            is_room_charge: false,
            is_fiscalised: true,
            items: [
              { item_row_name: 'ROW-1', item_code: 'BURGER', item_name: 'Burger', sold_qty: 2, returned_qty: 0, remaining_qty: 2, rate: 15000, amount: 30000 },
              { item_row_name: 'ROW-2', item_code: 'JUICE', item_name: 'Fresh Juice', sold_qty: 1, returned_qty: 0, remaining_qty: 1, rate: 15000, amount: 15000 },
            ],
            reasons: [{ code: 'RET-QUALITY', description: 'Item quality complaint', requires_note: 0, applies_to: ['return'] }],
          },
        };
      }
      if (method === 'ury.ury_pos.void_return_reopen.preview_pos_return') {
        return {
          message: {
            original_invoice: 'POS-INV-SETTLED-1',
            currency: 'UGX',
            precision: 0,
            items: [{ item_row_name: 'ROW-2', item_code: 'JUICE', item_name: 'Fresh Juice', qty: 1, rate: 15000, gross: 15000 }],
            subtotal: 15000,
            discount_amount: 0,
            tax_amount: 0,
            refund_total: 15000,
            max_cash_refundable: 15000,
            unpaid_credit_reduction: 0,
            default_refund_payments: [{ mode_of_payment: 'Cash', amount: 15000 }],
            refund_payments: [{ mode_of_payment: 'Cash', amount: 15000 }],
            is_room_charge: false,
            is_fiscalised: true,
          },
        };
      }
      if (method === 'ury.ury_pos.override.list_approvers') {
        return { message: [{ user: 'mgr@example.com', full_name: 'Alice Manager' }] };
      }
      if (method === 'ury.ury_pos.override.privilege_policy') {
        return { message: { privilege: 'return', label: 'Return / Refund', may_perform: false, requires_reason: false } };
      }
      return { message: [] };
    });

    vi.spyOn(call, 'post').mockImplementation(async (method: string, payload?: unknown) => {
      const body = (payload || {}) as Record<string, unknown>;
      if (method === 'ury.ury_pos.void_return_reopen.create_pos_return') {
        if (!body.approval_token) {
          throw new Error('A manager must approve this action. [POS-AUTH-REQUIRED]');
        }
        return {
          message: {
            return_invoice: 'POS-CN-901',
            original_invoice: 'POS-INV-SETTLED-1',
            currency: 'UGX',
            refund_total: 15000,
            refund_payments: [{ mode_of_payment: 'Cash', amount: 15000 }],
            approver: 'mgr@example.com',
            reason: 'RET-QUALITY',
            is_fiscalised: true,
            efris_creditnote_reasoncode: '101:Return of products due to expiry or damage, etc.',
          },
        };
      }
      if (method === 'ury.ury_pos.override.authorise') {
        return { message: { token: 'tok-orders-ret-1', approver: 'mgr@example.com', approver_name: 'Alice Manager', expires_in: 120 } };
      }
      return { message: null };
    });

    render(
      <MemoryRouter>
        <Orders />
      </MemoryRouter>,
    );

    // Both Return / Refund and Reopen / Adjust appear on POS Orders for settled checks
    const returnBtn = screen.getByRole('button', { name: /return \/ refund/i });
    expect(returnBtn).toBeTruthy();
    expect(screen.getByRole('button', { name: /reopen \/ adjust/i })).toBeTruthy();

    // Execute a partial return of the Add-Check second line (Fresh Juice) with manager PIN
    fireEvent.click(returnBtn);
    await screen.findByText(/Fresh Juice/);
    fireEvent.click(screen.getByRole('button', { name: /increase.*Fresh Juice/i }));
    await screen.findByText(/15,000/);

    fireEvent.change(screen.getByLabelText(/return reason/i), { target: { value: 'RET-QUALITY' } });
    fireEvent.click(screen.getByRole('button', { name: /process return/i }));

    await screen.findByText(/Alice Manager/);
    for (const d of ['7', '4', '1', '2']) {
      fireEvent.click(screen.getByRole('button', { name: d }));
    }
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));

    // Return receipt / credit note is automatically sent to the #144/#146 print format with skipStatusUpdate
    await waitFor(() =>
      expect(printMocks.printOrder).toHaveBeenCalledWith(
        expect.objectContaining({
          orderId: 'POS-CN-901',
          skipStatusUpdate: true,
        }),
      ),
    );
    expect(orderState.fetchOrders).toHaveBeenCalled();
  });
});
