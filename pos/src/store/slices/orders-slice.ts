import { getSessionUser, ownedSessionKey } from '@ury/core';
import { cachedOfflineProfile } from '../../pwa/register-offline';
import { StateCreator } from 'zustand';
import { OrderType, OrderStatusType } from '../../data/order-types';
import { isLinkDown, call } from '@ury/core';
import { getTillBranch, getPOSInvoices, getPOSInvoiceItems, getSplitGroup, mapSplitGroupInvoiceToPOSInvoice, POSInvoiceItem, POSInvoicePayment, POSInvoiceTax } from '../../lib/invoice-api';
import { searchPosInvoice } from '../../lib/invoice-api';
import { getCheckDiscounts, type CheckDiscountSummary } from '../../lib/discount-api';

export interface POSInvoice {
  branch?: string;
  ury_client_order_id?: string | null;
  name: string;
  invoice_printed: number;
  custom_printing_time?: string | null;
  grand_total: number;
  restaurant_table: string | null;
  cashier: string;
  waiter: string;
  /** the waiter's full name, when a caller has resolved it */
  waiter_name?: string | null;
  net_total: number;
  posting_time: string;
  total_taxes_and_charges: number;
  customer: string;
  customer_name?: string;
  custom_guest_label?: string | null;
  /** covers (guest count) on the check */
  no_of_pax?: number | null;
  creation?: string;
  status: 'Draft' | 'Unbilled' | 'Recently Paid' | 'Paid' | 'Consolidated' | 'Return';
  mobile_number: string;
  posting_date: string;
  rounded_total: number;
  order_type: OrderType;
  custom_merged_tables?: string | null;
  custom_split_group?: string | null;
  custom_split_from?: string | null;
  split_index?: number;
  split_total?: number;
  split_siblings?: string[];
  custom_merged_pos_invoice?: string | null;
  custom_merged_total?: number | null;
  additional_discount_percentage?: number;
  discount_amount?: number;
  coupon_code?: string | null;
  payments?: POSInvoicePayment[];
  room_number?: string | null;
  age_minutes?: number;
  items_preview?: Array<{ item_name: string; qty: number }>;
}

export interface OrdersState {
  ordersOwner: string | null;
  ordersBranch: string | null;
  ordersOffline: boolean;
  orders: POSInvoice[];
  orderLoading: boolean;
  error: string | null;
  pagination: {
    currentPage: number;
    hasNextPage: boolean;
    itemsPerPage: number;
  };
  selectedStatus: OrderStatusType;
  selectedOrder: POSInvoice | null;
  selectedOrderItems: POSInvoiceItem[];
  selectedOrderTaxes: POSInvoiceTax[];
  selectedOrderDiscounts: CheckDiscountSummary | null;
  selectedOrderLoading: boolean;
  selectedOrderError: string | null;
  orderSearchQuery: string;
}

export interface OrdersActions {
  fetchOrders: (page?: number) => Promise<void>;
  updateOrderStatus: (orderId: string, status: POSInvoice['status']) => Promise<void>;
  goToNextPage: () => Promise<void>;
  goToPreviousPage: () => Promise<void>;
  setSelectedStatus: (status: OrderStatusType) => Promise<void>;
  selectOrder: (order: POSInvoice) => Promise<boolean>;
  clearSelectedOrder: () => void;
  setOrderSearchQuery: (query: string) => void;
}

export type OrdersSlice = OrdersState & OrdersActions;

const ITEMS_PER_PAGE = 10;

export const createOrdersSlice: StateCreator<
  OrdersSlice,
  [],
  [],
  OrdersSlice
> = (set, get) => {
  let listVersion = 0;
  let detailVersion = 0;
  const emptyDetails = { selectedOrder: null, selectedOrderItems: [], selectedOrderTaxes: [], selectedOrderDiscounts: null, selectedOrderError: null, selectedOrderLoading: false };
  const clearQueue = (owner: string | null, branch: string | null) => {
    ++detailVersion;
    set({ ...emptyDetails, orders: [], ordersOwner: owner, ordersBranch: branch,
      pagination: { currentPage: 1, hasNextPage: false, itemsPerPage: ITEMS_PER_PAGE } });
  };
  return {
    ordersOwner: null,
    ordersBranch: null,
    // Initial state
    ordersOffline: false,
    orders: [],
    orderLoading: false,
    error: null,
    pagination: {
      currentPage: 1,
      hasNextPage: false,
      itemsPerPage: ITEMS_PER_PAGE,
    },
    selectedStatus: 'Outstanding',
    selectedOrder: null,
    selectedOrderItems: [],
    selectedOrderTaxes: [],
    selectedOrderDiscounts: null,
    selectedOrderLoading: false,
    selectedOrderError: null,
    orderSearchQuery: '',

    // Actions
    fetchOrders: async (page = 1) => {
      const version = ++listVersion;
      const owner = getSessionUser();
      const current = () => {
        if (version !== listVersion) return false;
        if (owner === getSessionUser()) return true;
        clearQueue(getSessionUser(), null);
        set({ orderLoading: false });
        return false;
      };
      if (get().ordersOwner !== owner) clearQueue(owner, null);
      let branch: string | undefined;
      try {
        set({ orderLoading: true, error: null, ordersOffline: false });
        const { orderSearchQuery, selectedStatus } = get();

        // Get POS profile to access paid_limit
        const key = ownedSessionKey('posProfile');
        const posProfile = key && sessionStorage.getItem(key);
        const profile = posProfile ? JSON.parse(posProfile) : null;
        const paidLimit = profile?.paid_limit;

        if (!owner || owner === 'Guest') throw new Error('Till session unavailable');
        if (selectedStatus === 'Outstanding') {
          branch = await getTillBranch();
          if (!current()) return;
          if (get().ordersBranch !== branch) {
            // Register can select its acknowledged invoice before the first queue page loads.
            if (get().selectedOrder?.branch === branch && !get().ordersBranch) set({ ordersBranch: branch });
            else clearQueue(owner, branch);
          }
        }
        if (orderSearchQuery && orderSearchQuery.trim()) {
          // Use search API
          const res = await searchPosInvoice(orderSearchQuery, selectedStatus, branch);
          if (!current()) return;
          set({
            orders: res.data || [],
            pagination: {
              currentPage: 1,
              hasNextPage: false,
              itemsPerPage: ITEMS_PER_PAGE,
            },
            orderLoading: false
          });
          return;
        }
        // Default fetch
        const limitStart = (page - 1) * ITEMS_PER_PAGE;
        const status = selectedStatus;
        const { invoices, hasMore } = await getPOSInvoices({
          status,
          limit: ITEMS_PER_PAGE,
          limit_start: limitStart,
          branch, paid_limit: paidLimit
        });
        if (!current()) return;
        set({
          orders: invoices,
          pagination: {
            currentPage: page,
            hasNextPage: hasMore,
            itemsPerPage: ITEMS_PER_PAGE,
          },
          orderLoading: false
        });
      } catch (error) {
        if (!current()) return;
        if (get().selectedStatus === 'Outstanding' && !branch) clearQueue(owner, null);
        if (isLinkDown(error) && cachedOfflineProfile(getSessionUser())) {
          set({ ordersOffline: true, orderLoading: false, orders: [], selectedOrder: null, error: null });
          return;
        }
        set({
          error: error instanceof Error ? error.message : 'Failed to fetch orders',
          orderLoading: false
        });
      }
    },

    goToNextPage: async () => {
      const { pagination, orderLoading } = get();
      if (!orderLoading && pagination.hasNextPage) {
        await get().fetchOrders(pagination.currentPage + 1);
      }
    },

    goToPreviousPage: async () => {
      const { pagination, orderLoading } = get();
      if (!orderLoading && pagination.currentPage > 1) {
        await get().fetchOrders(pagination.currentPage - 1);
      }
    },

    setSelectedStatus: async (status) => {
      set({ selectedStatus: status });
      // Clear selected order when status changes
      get().clearSelectedOrder();
      await get().fetchOrders(1); // Reset to first page when status changes
    },

    selectOrder: async (order) => {
      const owner = getSessionUser();
      if (get().ordersOwner !== owner) clearQueue(owner, order.branch ?? null);
      const version = ++detailVersion;
      if (!owner || owner === 'Guest') { set(emptyDetails); return false; }
      const current = () => {
        if (version !== detailVersion || get().selectedOrder?.name !== order.name) return false;
        if (owner === getSessionUser()) return true;
        clearQueue(getSessionUser(), null);
        return false;
      };
      try {
        set({
          ...emptyDetails, selectedOrder: order,
          selectedOrderLoading: true,
          selectedOrderError: null
        });

        const [{ items, taxes, payments }, splitGroup, checkDiscounts] = await Promise.all([
          getPOSInvoiceItems(order.name),
          getSplitGroup(order.name).catch(() => ({ invoices: [], current: order.name, group: null })),
          getCheckDiscounts(order.name).catch(() => null),
        ]);

        if (!current()) return false;
        const splitMatch = splitGroup.invoices.find((inv) => inv.name === order.name);
        const mergedOrder = splitMatch
          ? { ...order, ...mapSplitGroupInvoiceToPOSInvoice(splitMatch) }
          : order;
        const enrichedOrder = payments !== undefined
          ? { ...mergedOrder, payments }
          : mergedOrder;

        set((state) => ({
          selectedOrder: enrichedOrder,
          selectedOrderItems: items,
          selectedOrderTaxes: taxes,
          selectedOrderDiscounts: checkDiscounts,
          selectedOrderLoading: false,
          orders: splitMatch
            ? state.orders.map((o) => (o.name === enrichedOrder.name ? enrichedOrder : o))
            : state.orders,
        }));
        return true;
      } catch (error) {
        if (!current()) return false;
        set({
          selectedOrderError: error instanceof Error ? error.message : 'Failed to fetch order details',
          selectedOrderLoading: false
        });
        return false;
      }
    },

    clearSelectedOrder: () => {
      ++detailVersion;
      set(emptyDetails);
    },

    updateOrderStatus: async (orderId: string, status: POSInvoice['status']) => {
      try {
        set({ orderLoading: true, error: null });

        await call.post('ury.ury_pos.api.updatePosInvoiceStatus', {
          invoice: orderId,
          status,
        });

        // Refresh the orders list after status update
        await get().fetchOrders(get().pagination.currentPage);

        set({ orderLoading: false });
      } catch (error) {
        set({
          error: error instanceof Error ? error.message : 'Failed to update order status',
          orderLoading: false
        });
      }
    },

    setOrderSearchQuery: (query) => set({ orderSearchQuery: query }),
  };
};
