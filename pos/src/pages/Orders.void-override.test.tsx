import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { showToast } from '@ury/ui';
import { initI18n } from '../i18n';
import Orders from './Orders';

const state = vi.hoisted(() => ({
  selectOrder: vi.fn(),
  order: {
    name: 'POS-INV-900',
    status: 'Draft',
    invoice_printed: 1,
    grand_total: 50,
    rounded_total: 50,
    net_total: 50,
    total_taxes_and_charges: 0,
    restaurant_table: 'Table 4',
    cashier: 'cashier@example.com',
    waiter: 'cashier@example.com',
    customer: 'Walk In',
    mobile_number: '',
    posting_date: '2026-11-24',
    posting_time: '12:00:00',
    order_type: 'Dine In',
    age_minutes: 5,
  },
}));

vi.mock('../store/root-store', () => ({
  useRootStore: () => ({
    ordersOwner: 'cashier@example.com',
    orders: [state.order],
    selectedOrder: state.order,
    selectedOrderItems: [
      { name: 'ROW-FISH', item_code: 'FISH-01', item_name: 'Grilled Fish', qty: 2, rate: 25, price_list_rate: 25, amount: 50 },
    ],
    selectedOrderTaxes: [],
    selectedStatus: 'Draft',
    user: { name: 'cashier@example.com', full_name: 'Grace Cashier', roles: ['URY Cashier'] },
    orderLoading: false,
    selectedOrderLoading: false,
    error: null,
    selectedOrderError: null,
    pagination: { currentPage: 1, hasNextPage: false, hasPreviousPage: false },
    fetchOrders: vi.fn(),
    selectOrder: state.selectOrder,
    setSelectedStatus: vi.fn(),
    goToNextPage: vi.fn(),
    goToPreviousPage: vi.fn(),
    clearSelectedOrder: vi.fn(),
    orderSearchQuery: '',
  }),
}));

vi.mock('../store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: { name: 'Main Till', view_all_status: 1, paid_limit: 10 },
    paymentModes: ['Cash'],
    fetchPaymentModes: vi.fn(),
  }),
}));

vi.mock('../components/OrderStatusSidebar', () => ({ default: () => null }));
vi.mock('../components/OrderActionsMenu', () => ({ default: () => null }));
vi.mock('../components/SplitGroupPanel', () => ({ default: () => null }));
vi.mock('../components/MergedBillPanel', () => ({ default: () => null }));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  document.cookie = 'user_id=cashier%40example.com; Path=/';
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  state.selectOrder.mockReset();
  await initI18n('en');

  vi.spyOn(call, 'get').mockImplementation(async (method: string) => {
    if (method === 'frappe.client.has_permission') return { message: { has_permission: false } };
    if (method === 'ury.ury_pos.override.list_approvers') {
      return { message: [{ user: 'mgr@example.com', full_name: 'Alice Manager' }] };
    }
    if (method === 'ury.ury_pos.override.privilege_policy') {
      return { message: { privilege: 'void_sent_item', label: 'Void Sent Item', may_perform: false, requires_reason: true } };
    }
    if (method === 'ury.ury_pos.override.list_reasons') {
      return { message: [{ code: 'VOID-GUEST', description: 'Guest changed mind', requires_note: 0, applies_to: ['void_sent_item'] }] };
    }
    return { message: [] };
  });

  vi.spyOn(showToast, 'error').mockImplementation(() => '' as never);
  vi.spyOn(showToast, 'success').mockImplementation(() => '' as never);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Orders void_sent_item manager PIN flow', () => {
  it('opens ManagerApproval on POS-AUTH-REQUIRED and retries reduce_order_item_qty with token and reason', async () => {
    let reduceCalls = 0;
    const postSpy = vi.spyOn(call, 'post').mockImplementation(async (method: string, payload?: unknown) => {
      if (method === 'ury.ury.api.ury_pos_invoice_qty_reduction.reduce_order_item_qty') {
        reduceCalls += 1;
        const body = (payload || {}) as Record<string, unknown>;
        if (!body.approval_token) {
          throw new Error('A manager must approve this action. [POS-AUTH-REQUIRED]');
        }
        return {
          message: {
            invoice: 'POS-INV-900',
            item_row_name: 'ROW-FISH',
            item_code: 'FISH-01',
            previous_qty: 2,
            new_qty: 1,
            delta: -1,
            order_type: 'Dine In',
            cancel_kot_names: ['CNCL-KOT-1'],
            actor: 'cashier@example.com',
            approved_by: 'mgr@example.com',
          },
        };
      }
      if (method === 'ury.ury_pos.override.authorise') {
        return {
          message: {
            token: 'tok-void-1',
            approver: 'mgr@example.com',
            approver_name: 'Alice Manager',
            expires_in: 120,
          },
        };
      }
      return { message: null };
    });

    await act(async () => {
      root.render(
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Orders />
        </MemoryRouter>,
      );
    });

    const reduceBtn = container.querySelector<HTMLButtonElement>('button[aria-label="Reduce quantity of Grilled Fish"]');
    expect(reduceBtn).not.toBeNull();

    await act(async () => {
      reduceBtn!.click();
    });

    // ManagerApproval modal must open
    const dialog = await vi.waitFor(() => {
      const el = container.querySelector('[role="dialog"]');
      expect(el).not.toBeNull();
      return el!;
    });

    // Pick reason and enter 4-digit PIN
    const reasonSelect = await vi.waitFor(() => {
      const el = dialog.querySelector<HTMLSelectElement>('select[aria-label*="Reason" i]');
      expect(el).not.toBeNull();
      return el!;
    });

    await act(async () => {
      reasonSelect.value = 'VOID-GUEST';
      reasonSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });

    for (const digit of ['7', '4', '1', '2']) {
      const btn = [...dialog.querySelectorAll('button')].find((b) => b.textContent?.trim() === digit)!;
      await act(async () => btn.click());
    }

    const approveBtn = [...dialog.querySelectorAll('button')].find((b) => /approve/i.test(b.textContent || ''))!;
    await act(async () => approveBtn.click());

    await vi.waitFor(() => {
      expect(reduceCalls).toBe(2);
    });

    expect(postSpy).toHaveBeenCalledWith(
      'ury.ury_pos.override.authorise',
      expect.objectContaining({
        pos_profile: 'Main Till',
        privilege: 'void_sent_item',
        approver: 'mgr@example.com',
        pin_secret: '7412',
        reference_doctype: 'POS Invoice',
        reference_name: 'POS-INV-900',
        claim: expect.objectContaining({
          action: 'reduce_order_item_qty',
          item_row_name: 'ROW-FISH',
          item_code: 'FISH-01',
          from_qty: 2,
          to_qty: 1,
          amount: 25,
          reason: 'Guest changed mind',
        }),
      }),
    );

    expect(postSpy).toHaveBeenLastCalledWith(
      'ury.ury.api.ury_pos_invoice_qty_reduction.reduce_order_item_qty',
      expect.objectContaining({
        invoice_id: 'POS-INV-900',
        item_row_name: 'ROW-FISH',
        new_qty: 1,
        reason: 'Guest changed mind',
        approval_token: 'tok-void-1',
      }),
    );
    expect(state.selectOrder).toHaveBeenCalledWith(state.order);
  });
});
