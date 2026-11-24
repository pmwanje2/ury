import { qtyReductionMessage } from '../lib/qty-reduction-message';
import React, { useEffect, useMemo, useRef } from 'react';
import { Clock, User, UserCheck, Receipt, Printer, Pencil, X, GitBranch, GitMerge, Minus, Users, Utensils } from 'lucide-react';
import { Badge, Button, Card, CardContent, Select, SelectItem } from '@ury/ui';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@ury/ui';
import { showToast } from '@ury/ui';
import OrderStatusSidebar from '../components/OrderStatusSidebar';
import { useRootStore } from '../store/root-store';
import { formatCurrency, normalizeFrappeTime } from '@ury/core';
import { Spinner } from '@ury/ui';
import { Textarea } from '@ury/ui';
import { usePOSStore } from '../store/pos-store';
import SettledBills from './SettledBills';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import PaymentDialog from '../components/PaymentDialog';
import BillSplitDialog from '../components/BillSplitDialog';
import BillMergeDialog from '../components/BillMergeDialog';
import OrderActionsMenu from '../components/OrderActionsMenu';
import SplitGroupPanel from '../components/SplitGroupPanel';
import MergedBillPanel from '../components/MergedBillPanel';
import { printOrder } from '../lib/print';
import { call, getSessionUser } from '@ury/core';
import { splitBill, cancelOrder, reprintKot } from '../lib/order-api';
import PriceOverrideDialog from '../components/PriceOverrideDialog';
import CheckPrivilegedDialog, { type CheckPrivilegedTab } from '../components/CheckPrivilegedDialog';
import { extractAuthChallenge, isReasonRequiredError } from '../lib/privileged-actions-api';
import {
  getOrdersTabForInvoice,
  getOpenCheckCounts,
  getSplitGroup,
  getCombinedOrderTotals,
  isMergedBill,
  resolvePrintFormat,
  mapSplitGroupInvoiceToPOSInvoice,
  type POSInvoice,
  type SplitGroupInvoice,
} from '../lib/invoice-api';
import {
  reduceOrderItemQty,
  isOrderTypeNotAllowedError,
  isLastItemCannotBeRemovedError,
} from '../lib/order-api';
import { isAuthRequiredError, overrideErrorMessage } from '../lib/override-api';
import ManagerApproval, { type ManagerApprovalResult } from '../components/ManagerApproval';
import { parseFrappeError } from '@ury/core';
import { formatMergedTableLabel } from '../lib/table-utils';
import { t } from '../i18n';
import {
  getPrintWarningState,
  formatFrappeDatetime,
  type PrintWarningInput,
} from '../lib/invoice-print-warning';
import { orderTypeLabel } from '../data/order-types';
import { useSessionOwner } from '../lib/use-session-owner';
import BillRequestStrip from '../components/BillRequestStrip';
import Notifications from '../components/Notifications';
import { formatGuestSubtitle, formatStaffDisplayName } from '../lib/order-display';
import { useStaffNames } from '../lib/use-staff-names';
import type { OrderStatusType } from '../data/order-types';

function getOrderTableLabel(order: Pick<POSInvoice, 'restaurant_table' | 'custom_merged_tables'>) {
  if (!order.restaurant_table) return null;
  return formatMergedTableLabel(order.restaurant_table, order.custom_merged_tables);
}

// The check's guest label leads, so two splits on one walk-in customer
// ("Susan · Walk In", "David · Walk In") are told apart. The customer shows by
// name: on naming-series sites its id is "CUST-0001".
function checkName(order: Pick<POSInvoice, 'customer' | 'customer_name' | 'custom_guest_label'>) {
  return formatGuestSubtitle(order.custom_guest_label, order.customer_name, order.customer);
}

/** The waiter's User full name, else a name the payload carries, else one read from the id. */
function waiterName(order: Pick<POSInvoice, 'waiter' | 'waiter_name'>, staffNames: ReadonlyMap<string, string>) {
  return formatStaffDisplayName((order.waiter && staffNames.get(order.waiter)) || order.waiter_name, order.waiter);
}

function coversLabel(order: Pick<POSInvoice, 'no_of_pax'>) {
  const covers = Number(order.no_of_pax);
  return Number.isFinite(covers) && covers > 0 ? t('order.covers', { count: covers }) : null;
}

function tableAndOrderType(order: Pick<POSInvoice, 'restaurant_table' | 'custom_merged_tables' | 'order_type' | 'no_of_pax'>) {
  const table = getOrderTableLabel(order);
  const type = orderTypeLabel(order.order_type);
  const covers = coversLabel(order);
  return [table ? `${t('pos_ui.table')} ${table}` : null, type, covers].filter(Boolean).join(' · ');
}

/**
 * Open checks per open queue, re-read whenever the visible queue changes.
 * Counts that cannot be read are simply not shown, and none are read while
 * the queue is not this session's (enabled is false).
 */
function useOpenCheckCounts(enabled: boolean, branch: string | null, refreshKey: string) {
  const [counts, setCounts] = React.useState<Partial<Record<OrderStatusType, number>>>({});
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    getOpenCheckCounts(branch)
      .then((next) => { if (active) setCounts(next); })
      .catch(() => { if (active) setCounts({}); });
    return () => { active = false; };
  }, [enabled, branch, refreshKey]);
  return counts;
}

function isOrderEditable(status: string) {
  return status === 'Draft' || status === 'Unbilled';
}

const OTHER_VOID_REASON = '__other__';
const MIN_VOID_NOTE_LENGTH = 4;

/** POS Profile custom_void_reasons: one reason per line, blanks and repeats dropped. */
function configuredVoidReasons(value: unknown): string[] {
  if (typeof value !== 'string') return [];
  const reasons: string[] = [];
  for (const line of value.split(/\r?\n/)) {
    const reason = line.trim();
    if (!reason || reasons.includes(reason)) continue;
    reasons.push(reason);
  }
  return reasons;
}

function isSplitBill(order: Pick<POSInvoice, 'split_total' | 'custom_split_group' | 'custom_split_from'>) {
  return (
    (order.split_total ?? 0) >= 2 ||
    !!order.custom_split_group ||
    !!order.custom_split_from
  );
}

function LinkTag({ icon: Icon, children }: { icon: React.ComponentType<{ className?: string }>; children: React.ReactNode }) {
  return (
    <Badge size="tag" variant="tagAccent">
      <Icon className="h-2.5 w-2.5" />
      {children}
    </Badge>
  );
}

export default function Orders() {
  const owner = useSessionOwner();
  const { pathname } = useLocation();
  if (pathname.replace(/\/$/, '') === '/orders/settled') return <SettledBills key={owner} />;
  return <OwnerOrders key={owner} owner={owner} />;
}

function OwnerOrders({ owner }: { owner: string | null }) {
  const { 
    orders: list, ordersOwner, ordersBranch, ordersOffline,
    orderLoading,
    error,
    selectedStatus,
    pagination,
    selectedOrder: detail,
    selectedOrderItems,
    selectedOrderTaxes,
    selectedOrderLoading,
    selectedOrderError,
    fetchOrders,
    setSelectedStatus,
    goToNextPage,
    goToPreviousPage,
    selectOrder,
    clearSelectedOrder,
    orderSearchQuery
  } = useRootStore();

  const sameOwner = !!owner && owner !== 'Guest' && ordersOwner === owner;
  const orders = sameOwner ? list : [];
  const selectedOrder = sameOwner ? detail : null;
  const staffNames = useStaffNames([...orders.map((order) => order.waiter), selectedOrder?.waiter]);
  const openCheckCounts = useOpenCheckCounts(
    sameOwner,
    ordersBranch,
    `${selectedStatus}|${orders.map((order) => `${order.name}:${order.invoice_printed}`).join(',')}`,
  );
  const posStore = usePOSStore();
  const profileCurrency = posStore.posProfile?.currency;
  const selectedOrderCurrency = selectedOrder?.currency || profileCurrency;
  const printWarningMinutes = posStore.posProfile?.custom_invoice_warning_time ?? 0;
  const [clockNow, setClockNow] = React.useState(() => Date.now());
  useEffect(() => {
    if (!printWarningMinutes) return;
    const timer = window.setInterval(() => setClockNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [printWarningMinutes]);

  const renderPrintWarning = (order: PrintWarningInput) => {
    const warning = getPrintWarningState(order, printWarningMinutes, clockNow);
    if (!warning.exceeded) return null;
    return (
      <Badge variant="outline" className="shrink-0 border-red-500 text-red-600 bg-red-50">
        {warning.elapsedLabel}
      </Badge>
    );
  };
  const navigate = useNavigate();
  const mounted = useRef(false);
  const [cancelDialogOpen, setCancelDialogOpen] = React.useState(false);
  const [cancelReason, setCancelReason] = React.useState('');
  const [voidChoice, setVoidChoice] = React.useState('');
  const [otherNote, setOtherNote] = React.useState('');
  const voidReasons = configuredVoidReasons(posStore.posProfile?.custom_void_reasons);
  const [cancelLoading, setCancelLoading] = React.useState(false);
  const [editLoading, setEditLoading] = React.useState(false);
  const [showPaymentDialog, setShowPaymentDialog] = React.useState(false);
  const [showSplitDialog, setShowSplitDialog] = React.useState(false);
  const [showMergeDialog, setShowMergeDialog] = React.useState(false);
  const [orderActionsMenuOpen, setOrderActionsMenuOpen] = React.useState(false);
  const [isPrinting, setIsPrinting] = React.useState(false);
  const [canCancelInvoice, setCanCancelInvoice] = React.useState(false);
  const [reducingItemKey, setReducingItemKey] = React.useState<string | null>(null);
  const [pendingVoidApproval, setPendingVoidApproval] = React.useState<{
    order: POSInvoice;
    item: (typeof selectedOrderItems)[number];
    newQty: number;
    amount: number;
    claim: Record<string, unknown>;
  } | null>(null);
  const [priceOverrideItem, setPriceOverrideItem] = React.useState<(typeof selectedOrderItems)[number] | null>(null);
  const [checkPrivTab, setCheckPrivTab] = React.useState<CheckPrivilegedTab | null>(null);
  const [pendingOrderApproval, setPendingOrderApproval] = React.useState<{
    privilege: 'reprint_bill' | 'reprint_kot' | 'split_check';
    title: string;
    summaryLines: string[];
    referenceName: string;
    claim: Record<string, unknown>;
    requireReason?: boolean;
    retry: (res: ManagerApprovalResult) => Promise<void>;
  } | null>(null);

  React.useEffect(() => {
    if (selectedOrder?.name) {
      call.get('frappe.client.has_permission', { doctype: 'POS Invoice', docname: selectedOrder.name, perm_type: 'cancel' })
        .then((res: any) => {
          setCanCancelInvoice(res?.message?.has_permission === true);
        })
        .catch(() => {
          setCanCancelInvoice(false);
        });
    } else {
      setCanCancelInvoice(false);
    }
  }, [selectedOrder?.name]);

  const canSplitBill = useMemo(() => {
    if (!selectedOrder || selectedOrderItems.length === 0) return false;

    const hasSplittableItems =
      selectedOrderItems.length >= 2 ||
      selectedOrderItems.some((item) => item.qty > 1);
    if (!hasSplittableItems) return false;

    // Tab label (Unbilled/Draft) is a UI filter; doc status is always "Draft" for open bills.
    // Use invoice_printed to distinguish pre-print vs post-print split eligibility.
    const invoicePrinted = String(selectedOrder.invoice_printed);
    return invoicePrinted === '0' || invoicePrinted === '1';
  }, [selectedOrder, selectedOrderItems]);

  const isSecondaryMergedBill = useMemo(() => {
    if (!selectedOrder) return false;
    return orders.some((order) => order.custom_merged_pos_invoice === selectedOrder.name);
  }, [orders, selectedOrder]);

  const canMergeBill = useMemo(() => {
    if (!selectedOrder || selectedOrderItems.length === 0) return false;
    if (!isOrderEditable(selectedOrder.status)) return false;
    if (isMergedBill(selectedOrder)) return false;
    if (isSecondaryMergedBill) return false;
    return true;
  }, [selectedOrder, selectedOrderItems, isSecondaryMergedBill]);

  const selectedOrderTotals = useMemo(() => {
    if (!selectedOrder) {
      return { grandTotal: 0, roundedTotal: 0 };
    }
    return getCombinedOrderTotals(selectedOrder);
  }, [selectedOrder]);

  useEffect(() => {
    setOrderActionsMenuOpen(false);
  }, [selectedOrder?.name]);

  useEffect(() => {
    fetchOrders();
  }, [fetchOrders]);

  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return; // Skip the first run
    }
    fetchOrders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderSearchQuery]);


  // Function to format the date and time
  const formatDateTime = (date: string, time: string) => {
    const formattedDate = new Date(date + ' ' + normalizeFrappeTime(time)).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      hour12: true
    });
    return formattedDate;
  };

  const handleOrderClick = (order: any) => {
    selectOrder(order);
  };

  // Helper function to get badge variant based on order status
  const getBadgeVariant = (status: string) => {
    switch (status) {
      case 'Draft':
      case 'Unbilled':
        return 'secondary';
      case 'Recently Paid':
      case 'Paid':
      case 'Consolidated':
        return 'default';
      case 'Return':
        return 'destructive';
      default:
        return 'default';
    }
  };

  // The server re-validates the reason (T2a); this only refuses early.
  function voidReasonToSubmit(): string | null {
    if (voidReasons.length === 0) {
      const reason = cancelReason.trim();
      if (reason.length < MIN_VOID_NOTE_LENGTH) {
        showToast.error(t('errors.enter_cancel_reason'));
        return null;
      }
      return reason;
    }
    if (voidChoice === OTHER_VOID_REASON) {
      const note = otherNote.trim();
      if (note.length < MIN_VOID_NOTE_LENGTH) {
        showToast.error(t('errors.enter_cancel_reason'));
        return null;
      }
      return `Other: ${note}`;
    }
    if (!voidReasons.includes(voidChoice)) {
      showToast.error(t('errors.choose_void_reason'));
      return null;
    }
    return voidChoice;
  }

  function openCancelDialog() {
    setCancelReason('');
    setVoidChoice('');
    setOtherNote('');
    setCancelDialogOpen(true);
  }

  async function handleCancelOrder() {
    if (!selectedOrder) return;
    const reason = voidReasonToSubmit();
    if (!reason) return;
    setCancelLoading(true);
    try {
      await cancelOrder(selectedOrder.name, reason);
      showToast.success(t('success.order_cancelled'));
      setCancelDialogOpen(false);
      setCancelReason('');
      setVoidChoice('');
      setOtherNote('');
      clearSelectedOrder();
      fetchOrders();
    } catch (err) {
      showToast.error(parseFrappeError(err, t('errors.failed_cancel_order')));
    } finally {
      setCancelLoading(false);
    }
  }

  async function handleEditOrder() {
    if (!selectedOrder) return;
    setEditLoading(true);
    try {
      const res = await fetch(`/api/method/frappe.client.get?doctype=POS+Invoice&name=${selectedOrder.name}`);
      if (!res.ok) throw new Error('Failed to fetch order details');
      const data = await res.json();
      const order = data.message;
      if (order?.name !== selectedOrder.name || owner !== getSessionUser()) throw new Error(t('errors.failed_edit_order'));
      // Fill POS store
      posStore.resetOrderState();
      posStore.setSelectedOrderType(order.order_type);
      posStore.setOrderForUpdate(
        order.name,
        order.customer,
        order.modified,
        (order.items || []).map((item: any) => ({
          item_code: item.item_code,
          item_name: item.item_name,
          reservation_line_key: item.reservation_line_key || '',
          qty: Number(item.qty || 0),
          rate: Number(item.rate || 0),
          comment: item.comment || '',
          ury_held: Number(item.ury_held || 0),
        })),
      );
      if (order.restaurant_table) {
        posStore.setSelectedTable(order.restaurant_table, order.custom_restaurant_room || null,true);
      }
      posStore.setSelectedCustomer({ id: order.customer, name: order.customer_name, phone: order.mobile_number });
      posStore.setGuestLabel(order.custom_guest_label || '');
      if (order.no_of_pax != null && Number(order.no_of_pax) > 0) {
        posStore.setNoOfPax(Number(order.no_of_pax));
      }
      // Fill cart
      const items = (order.items || []).map((item: any) => ({
        id: item.item_code,
        name: item.item_name,
        price: item.rate,
        quantity: item.qty,
        amount: item.amount,
        image: item.image || null,
        uniqueId: item.name,
        reservationLineKey: item.reservation_line_key || item.name,
        item: item.item_code,
        item_name: item.item_name,
        item_image: null,
        course: item.custom_course || '',
        invoiceItemName: item.name,
        ury_held: item.ury_held || 0,
        comment: item.comment || '',
        description: item.description || '',
        special_dish: 0,
        tax_rate: 0,
      }));
      for (const cartItem of items) {
        await posStore.addToOrder(cartItem);
      }
      // Redirect to POS page
      navigate('/register');
    } catch (err) {
      showToast.error(parseFrappeError(err, t('errors.failed_edit_order')));
    } finally {
      setEditLoading(false);
    }
  }

  // Reduces a saved/printed order's line item by 1 (or removes it at qty 1)
  // via `reduce_order_item_qty` — a partial cancel-KOT is generated
  // server-side for the delta. When the server requires `void_sent_item`
  // authorisation (`POS-AUTH-REQUIRED`), opens `<ManagerApproval>` in place
  // and retries with the single-use approval token without changing session.
  async function handleReduceItemQty(
    item: (typeof selectedOrderItems)[number],
    approval?: { token: string; reason?: string; targetOrder?: POSInvoice; targetQty?: number }
  ) {
    const order = approval?.targetOrder ?? selectedOrder;
    if (!order) return;
    setReducingItemKey(item.name);
    const newQty = approval?.targetQty ?? item.qty - 1;
    try {
      const result = await reduceOrderItemQty(
        order.name,
        item.name,
        newQty,
        approval?.reason,
        approval?.token,
      );
      showToast.success(qtyReductionMessage(item.item_name, newQty, result.cancel_kot_names));
      await selectOrder(order);
    } catch (err) {
      const parsedMessage = parseFrappeError(err) || (err instanceof Error ? err.message : null);
      if (!approval?.token && isAuthRequiredError(parsedMessage || err)) {
        const deltaQty = item.qty - newQty;
        const amount = Math.round(Number(item.rate || 0) * deltaQty * 10000) / 10000;
        setPendingVoidApproval({
          order,
          item,
          newQty,
          amount,
          claim: {
            action: 'reduce_order_item_qty',
            item_row_name: item.name,
            item_code: item.item_code || '',
            from_qty: item.qty,
            to_qty: newQty,
            amount,
            reason: '',
          },
        });
      } else if (isOrderTypeNotAllowedError(parsedMessage)) {
        showToast.error(t('orders.qty_reduction_not_allowed', { order_type: orderTypeLabel(order.order_type) }));
      } else if (isLastItemCannotBeRemovedError(parsedMessage)) {
        showToast.error(t('orders.last_item_cannot_remove'));
      } else {
        showToast.error(overrideErrorMessage(err) || parsedMessage || 'Failed to reduce item quantity.');
      }
    } finally {
      setReducingItemKey(null);
    }
  }

  async function handleVoidApprovalGranted(result: ManagerApprovalResult) {
    const pending = pendingVoidApproval;
    setPendingVoidApproval(null);
    if (!pending) return;
    await handleReduceItemQty(pending.item, {
      token: result.token,
      reason: result.reason,
      targetOrder: pending.order,
      targetQty: pending.newQty,
    });
  }

  async function handlePrintOrder(approval?: ManagerApprovalResult) {
    if (!selectedOrder || !posStore.posProfile) return;
    setIsPrinting(true);
    try {
      await printOrder({
        orderId: selectedOrder.name,
        posProfile: posStore.posProfile,
        printFormat: resolvePrintFormat(selectedOrder, posStore.posProfile.print_format),
        approvalToken: approval?.token,
        reason: approval?.reason,
        reasonNote: approval?.note,
      });
      showToast.success(t('success.printed'));
      // Locally update selectedOrder.invoice_printed to 1
      if (selectedOrder && typeof selectedOrder === 'object') {
        const wasPrinted = Number(selectedOrder.invoice_printed) === 1;
        selectOrder({
          ...selectedOrder,
          invoice_printed: 1,
          custom_printing_time: wasPrinted
            ? selectedOrder.custom_printing_time ?? null
            : formatFrappeDatetime(new Date()),
        });
      }
      // If order was Unbilled, set to Draft and reload draft orders
      if (selectedStatus === 'Outstanding') {
        await fetchOrders();
      } else if (selectedStatus === 'Unbilled') {
        showToast.info(t('success.order_moved_to_draft'));
        setSelectedStatus('Draft');
        fetchOrders();
      }
    } catch (err: any) {
      if (!approval?.token && (isAuthRequiredError(err) || isReasonRequiredError(err))) {
        const orderName = selectedOrder.name;
        const challenge = extractAuthChallenge(err);
        setPendingOrderApproval({
          privilege: 'reprint_bill',
          title: t('privileged_actions.reprint_bill'),
          summaryLines: [`${t('privileged_actions.check_ref')}: ${orderName}`],
          referenceName: orderName,
          claim: challenge?.claim ?? { action: 'reprint_open_bill', invoice: orderName },
          requireReason: true,
          retry: (res) => handlePrintOrder(res),
        });
        return;
      }
      showToast.error(overrideErrorMessage(err) || t('errors.print_failed', { reason: err?.message || String(err) }));
    } finally {
      setIsPrinting(false);
    }
  }

  async function handleRefireKot(approval?: ManagerApprovalResult) {
    if (!selectedOrder) return;
    const orderName = selectedOrder.name;
    try {
      await reprintKot(
        orderName,
        approval ? { approvalToken: approval.token, reason: approval.reason, reasonNote: approval.note } : undefined,
      );
      showToast.success(t('privileged_actions.kot_refired'));
    } catch (err) {
      if (!approval?.token && (isAuthRequiredError(err) || isReasonRequiredError(err))) {
        const challenge = extractAuthChallenge(err);
        setPendingOrderApproval({
          privilege: 'reprint_kot',
          title: t('privileged_actions.reprint_kot'),
          summaryLines: [`${t('privileged_actions.check_ref')}: ${orderName}`],
          referenceName: orderName,
          claim: challenge?.claim ?? { action: 'reprint_kot', invoice: orderName },
          requireReason: true,
          retry: (res) => handleRefireKot(res),
        });
        return;
      }
      showToast.error(overrideErrorMessage(err));
    }
  }

  async function handleSplitBill(
    payload: {
      itemsToMove: Array<{ name: string; qty: number }>;
      customer?: string;
      guestLabel?: string;
    },
    approval?: ManagerApprovalResult,
  ) {
    if (!selectedOrder) return;
    const sourceName = selectedOrder.name;
    let result;
    try {
      result = await splitBill(
        sourceName,
        payload.itemsToMove,
        payload.customer,
        payload.guestLabel,
        approval ? { approvalToken: approval.token, reason: approval.reason, reasonNote: approval.note } : undefined,
      );
    } catch (err) {
      if (!approval?.token && isAuthRequiredError(err)) {
        const challenge = extractAuthChallenge(err);
        const sortedMoves = [...payload.itemsToMove]
          .sort((a, b) => a.name.localeCompare(b.name))
          .map((row) => [row.name, Math.round(row.qty * 10000) / 10000]);
        setPendingOrderApproval({
          privilege: 'split_check',
          title: t('privileged_actions.split_check'),
          summaryLines: [`${t('privileged_actions.check_ref')}: ${sourceName}`],
          referenceName: sourceName,
          claim: challenge?.claim ?? { action: 'split_bill', source_invoice: sourceName, move_items: sortedMoves },
          retry: (res) => handleSplitBill(payload, res),
        });
        return;
      }
      throw err;
    }
    showToast.success(t('bill_split.split_success', { invoice: result.new_invoice }));

    const childOnUnbilledTab =
      !!selectedOrder.restaurant_table &&
      String(selectedOrder.invoice_printed) === '1';

    if (childOnUnbilledTab && selectedStatus !== 'Outstanding') {
      await setSelectedStatus('Unbilled');
    } else {
      await fetchOrders();
    }

    const splitGroup = await getSplitGroup(result.new_invoice).catch(() => null);
    if (splitGroup?.invoices?.length) {
      const enriched = new Map(
        splitGroup.invoices.map((inv) => [inv.name, mapSplitGroupInvoiceToPOSInvoice(inv)])
      );
      useRootStore.setState((state) => ({
        orders: state.orders.map((o) => enriched.get(o.name) ?? o),
      }));
    }

    const newInvoice = splitGroup?.invoices.find((inv) => inv.name === result.new_invoice);
    if (newInvoice) {
      await selectOrder(mapSplitGroupInvoiceToPOSInvoice(newInvoice));
      return;
    }

    const newOrder = useRootStore.getState().orders.find((o) => o.name === result.new_invoice);
    if (newOrder) {
      await selectOrder(newOrder);
    }
  }

  async function handleMergeBill() {
    if (!selectedOrder) return;
    await fetchOrders();
    const updated = useRootStore.getState().orders.find((o) => o.name === selectedOrder.name);
    if (updated) {
      await selectOrder(updated);
    }
  }

  async function openSecondaryInvoice(invoiceName: string) {
    const inList = orders.find((o) => o.name === invoiceName);
    if (inList) {
      await selectOrder(inList);
      return;
    }
    await fetchOrders();
    const refreshed = useRootStore.getState().orders.find((o) => o.name === invoiceName);
    if (refreshed) {
      await selectOrder(refreshed);
    }
  }

  async function openRelatedInvoice(sibling: SplitGroupInvoice) {
    const tab = getOrdersTabForInvoice(sibling, {
      paidLimit: posStore.posProfile?.paid_limit,
      viewAllStatus: posStore.posProfile?.view_all_status,
    });

    if (tab !== selectedStatus) {
      await setSelectedStatus(tab);
    }

    const inList = useRootStore.getState().orders.find((o) => o.name === sibling.name);
    await selectOrder(inList ?? mapSplitGroupInvoiceToPOSInvoice(sibling));
  }

  async function selectBillRequest(invoice: POSInvoice) {
    // The store can change before React unmounts the previous branch’s strip.
    const live = useRootStore.getState();
    if (!ordersBranch || live.ordersBranch !== ordersBranch || live.ordersOwner !== owner) return false;
    return selectOrder(invoice);
  }

  async function openNotificationCheck(kotName: string, isCurrentSession: () => boolean) {
    const kot = await call.get<{ message: { invoice?: string } }>('frappe.client.get', { doctype: 'URY KOT', name: kotName });
    if (!isCurrentSession() || !kot.message?.invoice) return false;
    const response = await call.get<{ message: POSInvoice & { branch: string } }>('frappe.client.get', {
      doctype: 'POS Invoice', name: kot.message.invoice,
    });
    if (!isCurrentSession()) return false;
    const till = await call.get<{ message: string }>('ury.ury_pos.api.getBranch');
    if (!isCurrentSession() || !till.message || response.message?.branch !== till.message || response.message.name !== kot.message.invoice) return false;
    return await selectOrder(response.message) && isCurrentSession();
  }

  if (ordersOffline) {
    return <p className="p-4 text-center text-amber-800" role="status">{t('offline.orders_unavailable')}</p>;
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-3 lg:flex-row lg:overflow-hidden lg:[&>div:first-child]:w-48">
      {/* Left Sidebar - Order Types */}
      <div className="flex min-w-0 flex-col bg-white">
      <Link className="p-3 text-blue-700 underline" to="/orders/settled">{t('orders.settled_closed')}</Link>
      <OrderStatusSidebar
        selectedStatus={selectedStatus}
        setSelectedStatus={setSelectedStatus}
        getStatusCount={(status) => openCheckCounts[status]}
      />
      </div>

      {/* Middle Section - Order Cards */}
      <div className="flex min-w-0 flex-none flex-col lg:min-h-0 lg:flex-1">
        {/* Notifications sit beside the cards only once both fit (2xl): at
            1280px the 14rem rail squeezed every card to ~137px. */}
        <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_auto] gap-4 bg-gray-50 p-4 2xl:grid-cols-[minmax(0,1fr)_14rem] 2xl:grid-rows-1">
          <div className="min-w-0 overflow-y-auto">
          <BillRequestStrip ordersBranch={sameOwner ? ordersBranch : null} selectOrder={selectBillRequest} />
          {error ? (
            <div className="p-4 text-center">
              <p className="font-semibold text-red-600">{t('orders.failed_to_load')}</p>
              <p className="text-gray-600">{error}</p>
            </div>
          ) : orderLoading ? (
            <div className="flex items-center justify-center h-full">
              <Spinner  message={t('common.loading')} />
            </div>
          ) : orders.length === 0 ? (
            <div className="text-center mt-10">
              <p className="text-gray-500">{t('orders.no_orders_found')}</p>
            </div>
          ) : (
            // As many columns as fit, none narrower than 13rem, so every check
            // number stays readable whatever the screen and the rails beside it.
            <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,13rem),1fr))] gap-4">
              {orders.map((order) => {
                const splitBill = isSplitBill(order);
                const mergedBill = isMergedBill(order);
                const cardTotal = mergedBill
                  ? getCombinedOrderTotals(order).roundedTotal
                  : (order.rounded_total || order.grand_total || 0);

                return (
                <Card 
                  key={order.name} 
                  className={`p-0 bg-white hover:shadow-md transition-shadow flex flex-col overflow-hidden cursor-pointer ${
                    selectedOrder?.name === order.name ? 'ring-2 ring-blue-500 shadow-lg' : ''
                  } ${splitBill || mergedBill ? 'border-s-4 border-s-primary-500' : ''}`}
                  onClick={() => handleOrderClick(order)}
                >
                  <CardContent className="p-0 flex flex-col h-full">
                    <div className="p-3 bg-gray-50 border-b">
                    {/* The total drops under the reference only when both do not fit. */}
                    <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                      {/* Wraps rather than an ellipsis: a touch screen has no hover tooltip. */}
                      <h3 className="min-w-0 break-words text-sm font-semibold text-gray-900" title={order.name}>
                        {order.name}
                      </h3>
                      <span className="ms-auto shrink-0 whitespace-nowrap text-sm font-semibold tabular-nums text-gray-900">
                        {formatCurrency(cardTotal, order.currency || profileCurrency)}
                      </span>
                    </div>
                    <p className="mt-1 min-w-0 break-words text-xs text-gray-500">
                      {tableAndOrderType(order)}
                    </p>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <Badge variant="outline" className="shrink-0">
                        {t(String(order.invoice_printed) === '1' ? 'order.printed' : 'order.not_printed')}
                      </Badge>
                      <Badge variant={getBadgeVariant(order.status)} className="shrink-0">
                        {t(`order_status_types.${order.status.toLowerCase().replace(/ /g, '_')}`)}
                      </Badge>
                      {renderPrintWarning(order)}
                      {mergedBill && <LinkTag icon={GitMerge}>{t('bill_merge.merged_bill')}</LinkTag>}
                      {splitBill && (
                        <LinkTag icon={GitBranch}>
                          {(order.split_total ?? 0) >= 2
                            ? t('bill_split.split_indicator', {
                                index: order.split_index ?? 0,
                                total: order.split_total ?? 0,
                              })
                            : t('bill_split.split_bill')}
                        </LinkTag>
                      )}
                    </div>
                    {mergedBill && order.custom_merged_pos_invoice && (
                      <p className="mt-2 text-xs text-primary-700">
                        {t('bill_merge.includes_bill', { invoice: order.custom_merged_pos_invoice })}
                      </p>
                    )}
                    {splitBill && order.custom_split_from && (
                      <p className="mt-2 text-xs text-primary-700">
                        {t('bill_split.split_from', { invoice: order.custom_split_from })}
                      </p>
                    )}
                    </div>

                    {/* Content section - matches MenuCard padding and structure */}
                    <div className="flex-1 p-3 flex flex-col">
                      <p className="min-w-0 break-words text-sm text-gray-900">{checkName(order)}</p>

                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-gray-500">
                        <Clock className="h-3.5 w-3.5 shrink-0" />
                        <span className="whitespace-nowrap">{formatDateTime(order.posting_date, order.posting_time)}</span>
                        {order.age_minutes !== undefined && <span className="whitespace-nowrap">{t('order.age_minutes', { count: order.age_minutes })}</span>}
                      </div>
                      {waiterName(order, staffNames) && (
                        <p className="min-w-0 break-words text-xs text-gray-500" title={order.waiter || undefined}>
                          {waiterName(order, staffNames)}
                        </p>
                      )}
                      {order.items_preview?.length ? <p className="mt-2 text-sm text-gray-700">
                        {order.items_preview.map(item => `${item.qty}× ${item.item_name}`).join(', ')}
                      </p> : null}
                    </div>
                  </CardContent>
                </Card>
                );
              })}
            </div>
          )}
          {/* Pagination Controls */}
          {!orderLoading && (
            <div className="py-4">
              <div className="flex justify-center items-center gap-x-4 max-w-screen-xl mx-auto">
                <Button
                  onClick={goToPreviousPage}
                  disabled={pagination.currentPage === 1}
                  variant="outline"
                  className='w-20'
                  size="xs"
                >
                  {t('orders.pagination.previous')}
                </Button>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground">
                    {t('orders.pagination.page', { number: pagination.currentPage.toString() })}
                  </span>
                </div>
                <Button
                  onClick={goToNextPage}
                  disabled={!pagination.hasNextPage}
                  variant="outline"
                  className='w-20'
                  size="xs"
                >
                  {t('orders.pagination.next')}
                </Button>
              </div>
            </div>
          )}
          </div>
          <Notifications title="Notifications" onOpenCheck={openNotificationCheck} />
        </div>
      </div>

      {/* Right Section - Order Details */}
      <div className="flex min-h-96 min-w-0 flex-col rounded-lg border border-border bg-white lg:min-h-0 lg:w-[38%]">
        {!selectedOrder ? (
          <div className="text-center h-full flex flex-col items-center justify-center text-gray-500 p-6">
            <p className="text-lg font-medium mb-2">{t('order.select_to_view')}</p>
            <p className="text-sm">{t('orders.click_to_view')}</p>
          </div>
        ) : selectedOrderLoading ? (
          <div className="flex items-center justify-center h-full">
            <Spinner  message={t('common.loading')} />
          </div>
        ) : selectedOrderError ? (
          <div className="text-center h-full flex flex-col items-center justify-center text-red-500 p-6">
            <p className="text-lg font-medium mb-2">{t('orders.failed_to_load_details')}</p>
            <p className="text-sm">{selectedOrderError}</p>
          </div>
        ) : (
          <>
            {/* Fixed Header */}
            <div className="sticky top-0 start-0 end-0 z-20 border-b border-border bg-white px-6 py-4">
              <div className="flex items-center justify-between gap-2">
                <h2 className="min-w-0 flex-1 break-words text-lg font-semibold text-gray-900">
                  {selectedOrder.name}
                </h2>
                <div className="flex shrink-0 items-center gap-2">
                  {isOrderEditable(selectedOrder.status) && (
                    <>
                      <OrderActionsMenu
                        isOpen={orderActionsMenuOpen}
                        onOpenChange={setOrderActionsMenuOpen}
                        showMergeBill={canMergeBill}
                        onMergeBill={() => setShowMergeDialog(true)}
                        showSplitBill={canSplitBill}
                        onSplitBill={() => setShowSplitDialog(true)}
                        showComplimentary={selectedOrderItems.length > 0}
                        onComplimentary={() => setCheckPrivTab('complimentary')}
                        showTaxExempt={selectedOrderItems.length > 0}
                        onTaxExempt={() => setCheckPrivTab('tax_exempt')}
                        showServiceCharge={selectedOrderItems.length > 0}
                        onServiceCharge={() => setCheckPrivTab('service_charge')}
                        showRefireKot={selectedOrderItems.length > 0}
                        onRefireKot={() => void handleRefireKot()}
                      />
                      <button
                        type="button"
                        className="inline-flex items-center justify-center rounded-md p-2 bg-gray-100 hover:bg-gray-200 text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        aria-label={t('orders.edit_order')}
                        onClick={handleEditOrder}
                        disabled={editLoading}
                      >
                        <Pencil className="w-4 h-4" />
                        {editLoading && <span className="ms-2 text-xs">{t('common.loading')}</span>}
                      </button>
                      {canCancelInvoice && (
                        <button
                          type="button"
                          className="inline-flex items-center justify-center rounded-md p-2 bg-gray-100 hover:bg-gray-200 text-red-600 focus:outline-none focus:ring-2 focus:ring-red-500"
                          aria-label={t('orders.cancel_order')}
                          onClick={openCancelDialog}
                        >
                          <X className="w-4 h-4" />
                        </button>
                      )}
                    </>
                  )}
                  <Badge variant={getBadgeVariant(selectedOrder.status)}>
                    {t(`order_status_types.${selectedOrder.status.toLowerCase().replace(/ /g, '_')}`)}
                  </Badge>
                </div>
              </div>
              {/* Print state, then split/merge links, on their own row so the check number keeps its line. */}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Badge variant="outline">
                  {t(String(selectedOrder.invoice_printed) === '1' ? 'order.printed' : 'order.not_printed')}
                </Badge>
                {(selectedOrder.split_total ?? 0) >= 2 || isSplitBill(selectedOrder) ? (
                  <LinkTag icon={GitBranch}>
                    {(selectedOrder.split_total ?? 0) >= 2
                      ? t('bill_split.split_indicator', {
                          index: selectedOrder.split_index ?? 0,
                          total: selectedOrder.split_total ?? 0,
                        })
                      : t('bill_split.split_bill')}
                  </LinkTag>
                ) : null}
                {isMergedBill(selectedOrder) ? (
                  <LinkTag icon={GitMerge}>
                    {t('bill_merge.merged_bill')}
                  </LinkTag>
                ) : null}
              </div>
            </div>
            {/* Cancel Order Dialog */}
            <Dialog open={cancelDialogOpen} onOpenChange={setCancelDialogOpen}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t('order.cancel_order')}</DialogTitle>
                  <DialogDescription>
                    {voidReasons.length > 0
                      ? t('errors.choose_void_reason')
                      : t('errors.enter_cancel_reason')}
                  </DialogDescription>
                </DialogHeader>
                <div className="px-6 mb-3 space-y-3">
                {voidReasons.length > 0 ? (
                  <>
                    <Select
                      value={voidChoice}
                      onValueChange={setVoidChoice}
                      placeholder={t('order.select_void_reason')}
                      disabled={cancelLoading}
                      autoFocus
                    >
                      {voidReasons.map((reason) => (
                        <SelectItem key={reason} value={reason}>{reason}</SelectItem>
                      ))}
                      <SelectItem value={OTHER_VOID_REASON}>{t('order.void_reason_other')}</SelectItem>
                    </Select>
                    {voidChoice === OTHER_VOID_REASON && (
                      <Textarea
                        placeholder={t('order.enter_cancel_reason')}
                        value={otherNote}
                        onChange={e => setOtherNote(e.target.value)}
                        disabled={cancelLoading}
                      />
                    )}
                  </>
                ) : (
                <Textarea
                  placeholder={t('order.enter_cancel_reason')}
                  value={cancelReason}
                  onChange={e => setCancelReason(e.target.value)}
                  disabled={cancelLoading}
                  autoFocus
                />
                )}
                </div>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setCancelDialogOpen(false)} disabled={cancelLoading}>
                    {t('common.cancel')}
                  </Button>
                  <Button variant="danger" onClick={handleCancelOrder} disabled={cancelLoading}>
                    {cancelLoading ? t('common.cancelling') : t('common.confirm_cancel')}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
            {/* Scrollable Content Area */}
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <SplitGroupPanel
                invoiceName={selectedOrder.name}
                onOpenInvoice={openRelatedInvoice}
              />

              <MergedBillPanel
                order={selectedOrder}
                onOpenSecondary={openSecondaryInvoice}
              />

              <div className="mb-6">
                <div className="grid grid-cols-2 gap-4">
                  {/* First column: check name, open time and order type */}
                  <div className="min-w-0 space-y-3">
                    <div className="flex min-w-0 items-center gap-3 text-sm">
                      <User className="w-4 h-4 shrink-0 text-gray-500" />
                      <span className="min-w-0 break-words text-gray-900 font-medium">{checkName(selectedOrder)}</span>
                    </div>
                    <div className="flex items-center gap-3 text-sm">
                      <Clock className="w-4 h-4 shrink-0 text-gray-500" />
                      <span className="text-gray-600">{formatDateTime(selectedOrder.posting_date, selectedOrder.posting_time)}</span>
                    </div>
                    <div className="flex min-w-0 items-center gap-3 text-sm">
                      <Utensils className="w-4 h-4 shrink-0 text-gray-500" />
                      <span className="min-w-0 break-words text-gray-600">{orderTypeLabel(selectedOrder.order_type)}</span>
                    </div>
                  </div>
                  {/* Second column: waiter, table and covers */}
                  <div className="min-w-0 space-y-3">
                    {waiterName(selectedOrder, staffNames) && (
                      <div className="flex min-w-0 items-start gap-3 text-sm">
                        <UserCheck className="mt-0.5 w-4 h-4 shrink-0 text-gray-500" />
                        <div className="min-w-0">
                          <p className="break-words text-gray-600">{waiterName(selectedOrder, staffNames)}</p>
                          {/* The user id as text: a touch screen cannot hover for it. */}
                          {selectedOrder.waiter && selectedOrder.waiter !== waiterName(selectedOrder, staffNames) && (
                            <p className="break-all text-xs text-gray-400">{selectedOrder.waiter}</p>
                          )}
                        </div>
                      </div>
                    )}
                    {selectedOrder.restaurant_table && (
                      <div className="flex items-center gap-3 text-sm">
                        <Receipt className="w-4 h-4 shrink-0 text-gray-500" />
                        <span className="text-gray-600">{getOrderTableLabel(selectedOrder)}</span>
                      </div>
                    )}
                    {coversLabel(selectedOrder) && (
                      <div className="flex items-center gap-3 text-sm">
                        <Users className="w-4 h-4 shrink-0 text-gray-500" />
                        <span className="text-gray-600">{coversLabel(selectedOrder)}</span>
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Order Items */}
              <div className="mb-6">
                <h3 className="text-lg font-semibold text-gray-900 mb-4">{t('order.items_title')}</h3>
                <div className="space-y-3">
                  {/* qty === 0 rows are a fully-reduced item kept server-side
                      (zeroed, not deleted -- see ury_pos_invoice_qty_reduction's
                      full-removal note) and must stay hidden here. */}
                  {selectedOrderItems.filter((item) => !item.is_disposable && item.qty > 0).map((item, index) => {
                    const discountPercentage = item.rate < item.price_list_rate
                      ? Math.round(((item.price_list_rate - item.rate) / item.price_list_rate) * 100)
                      : null;
                    const canReduceQty =
                      String(selectedOrder.invoice_printed) === '1' &&
                      isOrderEditable(selectedOrder.status) &&
                      !isMergedBill(selectedOrder) &&
                      item.qty > 0;
                    const isReducingThisItem = reducingItemKey === item.name;

                    return (
                      <div key={index} className="flex justify-between items-start gap-3 py-2 border-b border-gray-100">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="break-words text-sm font-medium text-gray-900">{item.item_name}</p>
                            {discountPercentage !== null && (
                              <Badge className="shrink-0 bg-red-50 text-red-700 border-red-200 hover:bg-red-50 text-xs">
                                -{discountPercentage}%
                              </Badge>
                            )}
                          </div>
                          <div className="flex items-center gap-2 mt-0.5">
                            {canReduceQty && (
                              <button
                                type="button"
                                onClick={() => handleReduceItemQty(item)}
                                disabled={isReducingThisItem}
                                aria-label={`Reduce quantity of ${item.item_name}`}
                                className="flex items-center justify-center w-5 h-5 rounded-full border border-gray-300 text-gray-600 hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                {isReducingThisItem ? (
                                  <Spinner className="w-3 h-3" hideMessage message="" />
                                ) : (
                                  <Minus className="w-3 h-3" />
                                )}
                              </button>
                            )}
                            <p className="text-xs text-gray-500">{t('orders.qty_label', { qty: item.qty })}</p>
                          </div>
                        </div>
                        <div className="shrink-0 text-end tabular-nums">
                          <p className="text-sm font-semibold text-gray-900">
                            {formatCurrency(item.amount, selectedOrderCurrency)}
                          </p>
                          {isOrderEditable(selectedOrder.status) && !isMergedBill(selectedOrder) && (
                            <button
                              type="button"
                              onClick={() => setPriceOverrideItem(item)}
                              className="mt-0.5 text-xs font-medium text-blue-600 hover:underline"
                            >
                              {t('privileged_actions.price_override')}
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Taxes */}
              {selectedOrderTaxes.length > 0 && (
                <div className="mb-6">
                  <h3 className="text-lg font-semibold text-gray-900 mb-4">{t('order.taxes_charges')}</h3>
                  <div className="space-y-2">
                    {selectedOrderTaxes.map((tax, index) => (
                      <div key={index} className="flex justify-between items-center py-1">
                        <span className="text-sm text-gray-600">{tax.description}</span>
                        <span className="text-sm font-medium text-gray-900">
                          {formatCurrency(tax.rate, selectedOrderCurrency)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Sticky Bottom Section: Print | Split | Settle | Total */}
            <div className="border-t border-border p-6 bg-gray-50 sticky bottom-0 start-0 end-0 z-10">
              <div className="flex flex-wrap items-center gap-3 w-full">
                {/* Print Bill / Receipt Button */}
                <Button
                  variant="outline"
                  className="min-h-11 shrink-0 gap-2"
                  onClick={() => void handlePrintOrder()}
                  disabled={isPrinting}
                >
                  {isPrinting ? <Spinner className="w-5 h-5" hideMessage  message={t('common.loading')} /> : <Printer className="w-5 h-5" />}
                  {t(selectedOrder.status === 'Draft' || selectedOrder.status === 'Unbilled'
                    ? 'order.print_bill' : 'order.print_receipt')}
                </Button>
                {isOrderEditable(selectedOrder.status) && canSplitBill && (
                  <Button
                    variant="outline"
                    className="min-h-11"
                    onClick={() => setShowSplitDialog(true)}
                  >
                    {t('bill_split.split_bill')}
                  </Button>
                )}
                {/* Payment Button - Only show for Draft, Unbilled, and Recently Paid orders */}
                {isOrderEditable(selectedOrder.status) && (
                  <Button
                    className="min-h-11 flex-1"
                    onClick={() => {
                      const enablePaymentWithoutPrinting = Boolean(
                        Number(posStore.posProfile?.enable_payment_without_printing) === 1 ||
                        Number((posStore.posProfile as any)?.custom_enable_payment_without_printing) === 1
                      );
                      if (String(selectedOrder.invoice_printed) === '0' && !enablePaymentWithoutPrinting) {
                        showToast.error(t('errors.please_print_first'));
                        return;
                      }
                      setShowPaymentDialog(true);
                    }}
                  >
                    {t('order.settle_bill')}
                  </Button>
                )}
                {/* Total */}
                <span className="ms-auto text-2xl font-bold text-gray-900 whitespace-nowrap tabular-nums">
                  {isMergedBill(selectedOrder) ? (
                    <span className="flex flex-col items-end leading-tight">
                      <span className="text-xs font-medium text-gray-500">{t('bill_merge.combined_total')}</span>
                      <span>{formatCurrency(selectedOrderTotals.roundedTotal, selectedOrderCurrency)}</span>
                    </span>
                  ) : (
                    formatCurrency(selectedOrder.rounded_total || selectedOrder.grand_total || 0, selectedOrderCurrency)
                  )}
                </span>
              </div>
            </div>
          </>
        )}
      </div>
      {showPaymentDialog && selectedOrder && (
        <PaymentDialog
          order={selectedOrder}
          onClose={() => setShowPaymentDialog(false)}
          grandTotal={selectedOrderTotals.grandTotal}
          roundedTotal={selectedOrderTotals.roundedTotal}
          invoice={selectedOrder.name}
          customer={selectedOrder.customer}
          posProfile={posStore.posProfile?.name || ''}
          table={selectedOrder.restaurant_table || null}
          tableLabel={getOrderTableLabel(selectedOrder)}
          cashier={posStore.posProfile?.cashier || ''}
          owner={posStore.posProfile?.cashier || ''}
          fetchOrders={fetchOrders}
          clearSelectedOrder={clearSelectedOrder}
          discountPercentage={selectedOrder.additional_discount_percentage}
          discountAmount={selectedOrder.discount_amount}
        />
      )}
      {selectedOrder && (
        <BillMergeDialog
          open={showMergeDialog}
          onOpenChange={setShowMergeDialog}
          invoiceName={selectedOrder.name}
          posProfile={posStore.posProfile?.name || selectedOrder.pos_profile || ''}
          onConfirm={handleMergeBill}
        />
      )}
      {selectedOrder && (
        <BillSplitDialog
          open={showSplitDialog}
          onOpenChange={setShowSplitDialog}
          invoiceName={selectedOrder.name}
          items={selectedOrderItems}
          currency={selectedOrderCurrency}
          sourceCustomer={
            selectedOrder.customer
              ? { id: selectedOrder.customer, name: selectedOrder.customer_name || selectedOrder.customer, phone: selectedOrder.mobile_number }
              : null
          }
          onConfirm={handleSplitBill}
        />
      )}
      {pendingVoidApproval && (
        <ManagerApproval
          open
          posProfile={posStore.posProfile?.name || ''}
          privilege="void_sent_item"
          title={t('manager_approval.void_sent_item_title')}
          summaryLines={[
            t('manager_approval.void_sent_line', {
              item: pendingVoidApproval.item.item_name,
              from: pendingVoidApproval.item.qty,
              to: pendingVoidApproval.newQty,
            }),
            t('manager_approval.amount_line', { amount: formatCurrency(pendingVoidApproval.amount) }),
            t('manager_approval.check_line', { check: pendingVoidApproval.order.name }),
          ]}
          reference={{ doctype: 'POS Invoice', name: pendingVoidApproval.order.name }}
          claim={pendingVoidApproval.claim}
          amount={pendingVoidApproval.amount}
          onApproved={(res) => void handleVoidApprovalGranted(res)}
          onCancel={() => setPendingVoidApproval(null)}
        />
      )}
      {selectedOrder && priceOverrideItem && (
        <PriceOverrideDialog
          open
          onOpenChange={(next) => !next && setPriceOverrideItem(null)}
          mode="override"
          posProfile={posStore.posProfile?.name || ''}
          invoiceName={selectedOrder.name}
          itemRowName={priceOverrideItem.name}
          itemName={priceOverrideItem.item_name}
          currentRate={priceOverrideItem.rate}
          currency={selectedOrderCurrency}
          onSuccess={async () => {
            await fetchOrders();
            await selectOrder(selectedOrder);
          }}
        />
      )}
      {selectedOrder && checkPrivTab && (
        <CheckPrivilegedDialog
          open
          onOpenChange={(next) => !next && setCheckPrivTab(null)}
          initialTab={checkPrivTab}
          posProfile={posStore.posProfile?.name || ''}
          company={posStore.posProfile?.company || ''}
          invoiceName={selectedOrder.name}
          currency={selectedOrderCurrency}
          onSuccess={async () => {
            await fetchOrders();
            await selectOrder(selectedOrder);
          }}
        />
      )}
      {pendingOrderApproval && (
        <ManagerApproval
          open
          posProfile={posStore.posProfile?.name || ''}
          privilege={pendingOrderApproval.privilege}
          title={pendingOrderApproval.title}
          summaryLines={pendingOrderApproval.summaryLines}
          reference={{ doctype: 'POS Invoice', name: pendingOrderApproval.referenceName }}
          claim={pendingOrderApproval.claim}
          requireReason={pendingOrderApproval.requireReason}
          onApproved={(res) => {
            const pending = pendingOrderApproval;
            setPendingOrderApproval(null);
            void pending.retry(res);
          }}
          onCancel={() => setPendingOrderApproval(null)}
        />
      )}
    </div>
  );
};
