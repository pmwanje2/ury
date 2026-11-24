import { useEffect, useMemo, useState } from 'react';
import { Trash2, Edit, FrownIcon, Plus, Loader2, MessageSquare, AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react';
import { usePOSStore } from '../store/pos-store';
import { cn } from '@ury/ui';
import { formatCurrency, parseFrappeError, resolveDefaultCustomer } from '@ury/core';
import { CustomerSelect } from './CustomerSelect';
import ProductDialog from './ProductDialog';
import OrderTypeSelect from './OrderTypeSelect';
import CommentDialog from './CommentDialog';
import { Button, Badge } from '@ury/ui';
import { getOrderKotStatus, type KotItemState } from '../lib/kot-status-api';
import { Spinner } from '@ury/ui';
import { HoldFireControls } from './HoldFireControls';
import { useOutbox } from '../offline/use-outbox';
import { roundItems } from '../offline/outbox';
import { submitOrderOfflineAware } from '../offline/cashier';
import { syncOrder, type SavedInvoice, type SyncOrderRequest } from '../lib/order-api';
import { useOrderPreview } from '../lib/use-order-preview';
import { isAuthRequiredError, overrideErrorMessage } from '../lib/override-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { useRootStore } from '../store/root-store';
import type { RootState } from '../store/root-store';
import { showToast } from '@ury/ui';
import { DINE_IN, orderTypeLabel } from '../data/order-types';
import { t } from '../i18n';
import { cartQtyForItem, remainingHeadroom } from '../lib/cart-capacity';
import type { MenuAvailability } from '../lib/availability-api';

const KOT_BADGE: Record<KotItemState, { label: string; variant: 'cancelled' | 'warning' | 'info' | 'success' }> = {
  QUEUED: { label: 'order.kot_sent', variant: 'cancelled' },
  IN_PREPARATION: { label: 'order.kot_preparing', variant: 'warning' },
  READY: { label: 'order.kot_ready', variant: 'info' },
  SERVED: { label: 'order.kot_served', variant: 'success' },
};

const EMPTY_AVAILABILITY: MenuAvailability = {};

const OrderPanel = ({ onCashierCheck, availabilityMap: availabilityByItem = EMPTY_AVAILABILITY }: {
  onCashierCheck?: (invoice: SavedInvoice) => void;
  availabilityMap?: MenuAvailability;
}) => {
  const { 
    activeOrders, 
    removeFromOrder, 
    updateQuantity, 
    clearOrder, 
    setSelectedItem,
    orderLoading,
    isOrderInteractionDisabled,
    isUpdatingOrder,
    posProfile,
    currency: storeCurrency,
    selectedOrderType,
    selectedTable,
    selectedRoom,
    tableOrder, editCustomer,
    selectedCustomer,
    selectedAggregator,
    resetOrderState,
    paymentModes,
    orderId,
    originalSentItems = [],
    orderComment,
    setOrderComment,
    guestLabel,
    setGuestLabel,
    noOfPax,
    setNoOfPax,
    lastModifiedTime
  } = usePOSStore();
  const activeCurrency = posProfile?.currency || storeCurrency;
  const user = useRootStore((state: RootState) => state.user);
  const outbox = useOutbox(posProfile, user);
  const [offlineTarget, setOfflineTarget] = useState('');
  const target = outbox.rows.find((row) => row.client_order_id === offlineTarget && row.state === 'pending');
  const [editingItem, setEditingItem] = useState<typeof activeOrders[0] | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [showCommentDialog, setShowCommentDialog] = useState(false);
  const [pendingSyncVoidApproval, setPendingSyncVoidApproval] = useState<{
    orderData: SyncOrderRequest;
    amount: number;
    linesCount: number;
    claim: Record<string, unknown>;
  } | null>(null);

  // Read-only kitchen state per item_code for a running (saved) order.
  const [kotState, setKotState] = useState<Record<string, KotItemState>>({});
  useEffect(() => {
    if (!orderId) {
      setKotState({});
      return;
    }
    let cancelled = false;
    const load = () =>
      getOrderKotStatus(orderId)
        .then((r) => !cancelled && setKotState(r.items))
        .catch(() => {});
    load();
    const timer = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [orderId, lastModifiedTime]);

  const calculateItemTotal = (item: typeof activeOrders[0]) => {
    const basePrice = item.selectedVariant?.price || item.price;
    const addonsTotal = item.selectedAddons?.reduce((sum, addon) => sum + addon.price, 0) || 0;
    return (basePrice + addonsTotal) * item.quantity;
  };

  const total = activeOrders.reduce(
    (sum, item) => sum + calculateItemTotal(item),
    0
  );

  const [showMobileBreakdown, setShowMobileBreakdown] = useState(true);
  const previewCustomer =
    selectedOrderType === 'Aggregators'
      ? selectedAggregator?.customer
      : selectedCustomer?.id || undefined;
  const previewItems = useMemo(
    () =>
      activeOrders.map((item) => ({
        item: item.id,
        item_name: item.name,
        qty: item.quantity,
        rate: item.selectedVariant?.price || item.price,
      })),
    [activeOrders],
  );
  const { preview: orderPreview, loading: previewLoading } = useOrderPreview({
    enabled: activeOrders.length > 0 && Boolean(posProfile?.name),
    offline: Boolean(outbox.offline),
    debounceMs: 150,
    posProfile: posProfile?.name,
    orderType: selectedOrderType,
    customer: previewCustomer,
    items: previewItems,
    noOfPax,
    invoiceName: isUpdatingOrder ? orderId : undefined,
    room: selectedRoom,
    table: selectedTable,
  });
  const displayTotal =
    !outbox.offline && orderPreview
      ? (orderPreview.rounded_total || orderPreview.grand_total)
      : total;

  const handleEdit = (item: typeof activeOrders[0]) => {
    const menuItem = {
      ...item,
      variants: item.variants,
      addons: item.addons,
    };
    setSelectedItem(menuItem);
    setEditingItem(item);
  };

  const handleCommentSave = (comment: string) => {
    setOrderComment(comment);
  };

  const buildSyncVoidClaim = (orderData: SyncOrderRequest) => {
    const beforeMap = new Map<string, { item_code: string; line_key: string; qty: number; rate: number }>();
    const afterMap = new Map<string, number>();

    for (const row of originalSentItems) {
      if (Number(row.ury_held || 0) === 1) continue;
      const lineKey = row.reservation_line_key || '';
      const composite = JSON.stringify([row.item_code, row.comment || '', lineKey]);
      const prev = beforeMap.get(composite);
      if (prev) {
        prev.qty += Number(row.qty || 0);
      } else {
        beforeMap.set(composite, {
          item_code: row.item_code,
          line_key: lineKey,
          qty: Number(row.qty || 0),
          rate: Number(row.rate || 0),
        });
      }
    }

    for (const row of orderData.items) {
      if (Number(row.ury_held || 0) === 1) continue;
      const lineKey = row.reservation_line_key || '';
      const composite = JSON.stringify([row.item, row.comment || '', lineKey]);
      afterMap.set(composite, (afterMap.get(composite) || 0) + Number(row.qty || 0));
    }

    const sortedKeys = Array.from(beforeMap.keys()).sort();
    const lines: Array<{
      item_code: string;
      reservation_line_key: string;
      from_qty: number;
      to_qty: number;
      delta: number;
    }> = [];
    let amount = 0;

    for (const composite of sortedKeys) {
      const before = beforeMap.get(composite)!;
      const afterQty = afterMap.get(composite) || 0;
      if (afterQty < before.qty) {
        const delta = before.qty - afterQty;
        amount += before.rate * delta;
        lines.push({
          item_code: before.item_code,
          reservation_line_key: before.line_key,
          from_qty: before.qty,
          to_qty: afterQty,
          delta,
        });
      }
    }

    const roundedAmount = Math.round(amount * 10000) / 10000;
    return {
      amount: roundedAmount,
      linesCount: lines.length,
      claim: {
        action: 'sync_order_void_sent_item',
        lines,
        amount: roundedAmount,
        reason: '',
      },
    };
  };

  const submitSyncPayload = async (orderData: SyncOrderRequest, allowPromptOnAuthRequired: boolean) => {
    if (!posProfile || !user?.name) return;
    setIsSubmitting(true);
    try {
      const submission = await submitOrderOfflineAware({
        profile: posProfile,
        user,
        order: target ? { ...target.order, items: orderData.items } : orderData,
        total: total + (target ? target.rounds[target.rounds.length - 1].expected_total : 0),
        clientOrderId: target?.client_order_id,
        onlineSubmit: (ids) =>
          syncOrder({
            ...orderData,
            ...(ids && {
              client_order_id: ids.clientOrderId,
              round_id: ids.roundId,
              items: roundItems(orderData.items, ids.roundId),
            }),
          }),
      });
      const result = submission.offline ? null : submission.response;

      // sync_order returns { status: 'Failure' } instead of throwing when the
      // write is rejected (stale last_modified_time, table already occupied,
      // or the invoice was already billed by another user).
      if (
        result?.message &&
        typeof result.message === 'object' &&
        'status' in result.message &&
        result.message.status === 'Failure'
      ) {
        showToast.error(parseFrappeError(result, t('errors.order_sync_failed')));
        return;
      }

      // Reset all states after successful order submission
      resetOrderState();
      setOfflineTarget('');
      showToast.success(
        submission.offline
          ? t('offline.saved')
          : isUpdatingOrder
            ? t('success.order_updated')
            : t('success.order_created'),
      );
      if (result?.message && 'name' in result.message && user.roles?.includes('URY Cashier')) {
        onCashierCheck?.(result.message);
      }
    } catch (error) {
      if (allowPromptOnAuthRequired && isUpdatingOrder && orderId && isAuthRequiredError(error)) {
        const voidInfo = buildSyncVoidClaim(orderData);
        setPendingSyncVoidApproval({
          orderData,
          amount: voidInfo.amount,
          linesCount: voidInfo.linesCount,
          claim: voidInfo.claim,
        });
        return;
      }
      console.error('Failed to sync order:', error);
      showToast.error(overrideErrorMessage(error, 'errors.failed_process_order'));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSyncVoidApproved = async (res: ManagerApprovalResult) => {
    const pending = pendingSyncVoidApproval;
    setPendingSyncVoidApproval(null);
    if (!pending) return;
    await submitSyncPayload(
      {
        ...pending.orderData,
        approval_token: res.token,
        void_reason: res.reason,
      },
      false,
    );
  };

  const handleSubmit = async () => {
    try {
      if (!posProfile) {
        throw new Error(t('errors.pos_profile_not_found'));
      }

      if (!user?.name) {
        throw new Error(t('errors.user_not_logged_in'));
      }

      const routine = selectedOrderType === DINE_IN || selectedOrderType === 'Take Away';
      const invoiceCustomer = isUpdatingOrder && orderId ? (
        editCustomer?.invoice === orderId ? editCustomer.customer :
          tableOrder?.message?.name === orderId ? tableOrder.message.customer : null
      ) : null;
      const customer = selectedCustomer?.id || invoiceCustomer || (!isUpdatingOrder && outbox.offline && routine
        ? resolveDefaultCustomer(selectedOrderType, posProfile)?.id : '');
      // Validate customer/aggregator details
      if (!target && selectedOrderType === 'Aggregators') {
        if (!selectedAggregator?.customer) {
          showToast.error(t('errors.select_aggregator'));
          return;
        }
      } else if (!target && !customer && (!routine || outbox.offline)) {
        showToast.error(t('errors.select_customer'));
        return;
      }

      // Validate table selection for dine-in orders
      if (!target && selectedOrderType === DINE_IN && !selectedTable) {
        showToast.error(t('errors.select_table', { order_type: orderTypeLabel(DINE_IN) }));
        return;
      }

      const orderData: SyncOrderRequest = {
        items: activeOrders.map(item => ({
          ...(item.invoiceItemName ? { name: item.invoiceItemName } : {}),
          ...(posProfile.ury_enable_hold_fire || item.ury_held ? { ury_held: item.ury_held || 0 } : {}),
          item: item.id,
          item_name: item.name,
          rate: item.selectedVariant?.price || item.price,
          qty: item.quantity,
          comment: item.comment || undefined,
          // Stable line identity — without it the server cannot tell a
          // quantity change on an existing line from a remove + re-add, and
          // a 1 -> 2 bump cancels the original KOT and re-fires a full one
          // instead of raising a +1 delta (B02b).
          reservation_line_key: item.reservationLineKey || item.uniqueId,
        })),
        no_of_pax: noOfPax,
        pos_profile: posProfile.name,
        order_type: selectedOrderType,
        table: selectedTable || undefined,
        room: selectedRoom || undefined,
        customer: selectedOrderType === 'Aggregators' ? selectedAggregator?.customer : customer,
        aggregator_id: selectedOrderType === 'Aggregators' ? selectedAggregator?.customer : undefined,
        cashier: posProfile.cashier,
        owner: posProfile.owner,
        mode_of_payment: paymentModes[0],
        last_invoice: isUpdatingOrder ? orderId : null,
        last_modified_time: isUpdatingOrder ? (lastModifiedTime || undefined) : undefined,
        invoice: isUpdatingOrder ? orderId : null,
        waiter: user.name,
        comments: orderComment || undefined,
        // an existing order always sends its label, so clearing the box clears it
        ...(guestLabel?.trim() || isUpdatingOrder ? { guest_label: guestLabel?.trim() ?? '' } : {}),
      };

      await submitSyncPayload(orderData, true);
    } catch (error) {
      console.error('Failed to sync order:', error);
      showToast.error(parseFrappeError(error, t('errors.failed_process_order')));
    }
  };

  const MIN_PAX = 1;
  const MAX_PAX = 50;

  const handlePaxDecrement = () => {
    setNoOfPax(Math.max(MIN_PAX, noOfPax - 1));
  };

  const handlePaxIncrement = () => {
    setNoOfPax(Math.min(MAX_PAX, noOfPax + 1));
  };

  const EmptyCartUI = () => (
    <div className="flex-1 flex flex-col items-center justify-center p-8 text-center">
      <div className="w-24 h-24 bg-gray-100 rounded-full flex items-center justify-center mb-6">
        <FrownIcon className="w-12 h-12 text-gray-400" />
      </div>
      
      <h3 className="text-lg font-semibold text-gray-900 mb-2">
        {t('cart.empty_title')}
      </h3>

      <p className="text-gray-500 text-sm mb-6 max-w-xs leading-relaxed">
        {t('cart.empty_subtitle')}
      </p>

      <div className="flex items-center gap-2 text-blue-600 bg-blue-50 px-4 py-2 rounded-lg">
        <Plus className="w-4 h-4" />
        <span className="text-sm font-medium">{t('cart.click_to_add')}</span>
      </div>

      <div className="mt-4 text-xs text-gray-400">
        {t('cart.double_click_hint')}
      </div>
    </div>
  );

  const LoadingOrderUI = () => (
    <div className="h-96">
      <Spinner message={t('cart.loading_order')} />
    </div>
  );

  const isInteractionDisabled = isOrderInteractionDisabled() || isSubmitting;

  return (
    <div data-testid="order-panel" className="min-h-0 w-full flex-1 overflow-y-auto border-t border-border bg-white lg:flex lg:h-full lg:w-96 lg:flex-none lg:flex-col lg:overflow-hidden lg:border-s lg:border-t-0">
      <div className="min-h-0 overflow-y-auto border-b border-border p-4">
        {outbox.queue && outbox.offline && <label className="block text-sm mb-2">{t('offline.round_target')}
          <select data-testid="offline-round-target" className="block w-full border p-1" value={offlineTarget} disabled={isInteractionDisabled || outbox.replaying} onChange={(event) => setOfflineTarget(event.target.value)}>
            <option value="">{t('offline.new_order')}</option>
            {outbox.rows.filter((row) => row.state === 'pending' && !row.settlement && row.next_round === 0).map((row) => <option key={row.client_order_id} value={row.client_order_id}>{row.order.customer} · {row.client_order_id.slice(0, 8)}</option>)}
          </select>
        </label>}
        <OrderTypeSelect disabled={isInteractionDisabled} />
        <div className="mt-3"><CustomerSelect disabled={isInteractionDisabled} /></div>
        <div className="mt-3 flex items-center justify-between">
          <span className="text-sm font-medium text-gray-700">{t('cart.pax')}</span>
          <div className="flex items-center gap-2">
            <Button
              onClick={handlePaxDecrement}
              variant="outline"
              size="icon"
              className="w-8 h-8 rounded-full"
              disabled={isInteractionDisabled || noOfPax <= MIN_PAX}
            >
              -
            </Button>
            <span className="w-6 text-center">{noOfPax}</span>
            <Button
              onClick={handlePaxIncrement}
              variant="outline"
              size="icon"
              className="w-8 h-8 rounded-full"
              disabled={isInteractionDisabled || noOfPax >= MAX_PAX}
            >
              +
            </Button>
          </div>
        </div>
      </div>
      
      {orderLoading ? (
        <LoadingOrderUI />
      ) : activeOrders.length === 0 ? (
        <EmptyCartUI />
      ) : (
        <>
          <div className="min-h-0 flex-1 overflow-y-auto px-6">
            <HoldFireControls />
            {activeOrders.map((item) => (
              <div
                key={item.uniqueId}
                className={cn(
                  "flex flex-col py-4 border-b border-gray-100",
                  isInteractionDisabled && "opacity-50"
                )}
              >
                <div className="flex items-center justify-between">
                  <div className="flex-1">
                    <div className="flex items-center justify-between">
                      <h3 className="font-medium text-gray-900 text-sm">{item.name}</h3>
                      {orderId && kotState[item.item] && (
                        <Badge
                          size="sm"
                          variant={KOT_BADGE[kotState[item.item]].variant}
                          data-testid="kot-status-badge"
                        >
                          {t(KOT_BADGE[kotState[item.item]].label)}
                        </Badge>
                      )}
                    </div>
                    {item.selectedVariant && (
                      <p className="text-sm text-gray-600">{item.selectedVariant.name}</p>
                    )}
                    {item.selectedAddons && item.selectedAddons.length > 0 && (
                      <p className="text-sm text-gray-500">
                        {item.selectedAddons.map(addon => addon.name).join(', ')}
                      </p>
                    )}
                    <p className="text-gray-600 text-sm">{formatCurrency(calculateItemTotal(item), activeCurrency)}</p>
                  </div>
                  
                  <div className="flex items-center gap-2">
                    <Button
                      onClick={() => handleEdit(item)}
                      variant="ghost"
                      size="icon"
                      className="text-blue-600 hover:text-blue-700"
                      title={t('cart.edit_item')}
                      disabled={isInteractionDisabled}
                    >
                      <Edit className="w-4 h-4" />
                    </Button>
                    <div className="flex items-center gap-2">
                      <Button
                        onClick={() => {
                          const newQuantity = Math.max(0, Math.round((item.quantity - 1) * 1000) / 1000);
                          if (newQuantity <= 0) {
                            removeFromOrder(item.uniqueId!);
                          } else {
                            updateQuantity(item.uniqueId!, newQuantity);
                          }
                        }}
                        variant="outline"
                        size="icon"
                        className="w-8 h-8 rounded-full"
                        disabled={isInteractionDisabled}
                      >
                        -
                      </Button>
                      <span className="w-6 text-center">{item.quantity}</span>
                      {(() => {
                        const headroom = remainingHeadroom(
                          availabilityByItem[item.item],
                          cartQtyForItem(activeOrders, item.item),
                        );
                        const atCapacity = headroom !== undefined && headroom <= 0;
                        return (
                          <Button
                            onClick={() => updateQuantity(item.uniqueId!, Math.round((item.quantity + 1) * 1000) / 1000)}
                            variant="outline"
                            size="icon"
                            className="w-8 h-8 rounded-full"
                            disabled={isInteractionDisabled || atCapacity}
                            title={atCapacity ? t('cart.insufficient_capacity') : undefined}
                          >
                            +
                          </Button>
                        );
                      })()}
                    </div>
                    
                    <Button
                      onClick={() => removeFromOrder(item.uniqueId!)}
                      variant="ghost"
                      size="icon"
                      className="text-red-500 hover:text-red-600"
                      disabled={isInteractionDisabled}
                    >
                      <Trash2 className="w-5 h-5" />
                    </Button>
                  </div>
                </div>
              </div>
            ))}
            {activeOrders.length > 0 && (
              <Button
                onClick={clearOrder}
                variant="ghost"
                size="sm"
                className="w-full text-gray-600 hover:text-gray-800 mt-4"
                disabled={isInteractionDisabled}
              >
                {t('cart.clear_cart')}
              </Button>
            )}
          </div>
          
          <div
            data-testid="order-tax-preview-footer"
            className="sticky bottom-0 p-4 border-t border-border flex-shrink-0 bg-white lg:static"
          >
            <input
              type="text"
              name="guest_label"
              maxLength={40}
              value={guestLabel ?? ''}
              onChange={(e) => setGuestLabel(e.target.value)}
              placeholder={t('cart.guest_label')}
              aria-label={t('cart.guest_label')}
              className="mb-3 w-full rounded-md border border-border px-3 py-1.5 text-sm"
              disabled={isInteractionDisabled}
            />
            {outbox.offline ? (
              <div
                data-testid="offline-estimate-badge"
                className="mb-2 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-800"
              >
                {t('cart.offline_estimate')}
              </div>
            ) : orderPreview ? (
              <div className="mb-2 space-y-1 text-xs text-gray-600">
                <div className="flex items-center justify-between lg:hidden">
                  <button
                    type="button"
                    onClick={() => setShowMobileBreakdown((prev) => !prev)}
                    className="inline-flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-700"
                  >
                    <span>{t('cart.toggle_breakdown')}</span>
                    {showMobileBreakdown ? (
                      <ChevronUp className="h-3.5 w-3.5" aria-hidden />
                    ) : (
                      <ChevronDown className="h-3.5 w-3.5" aria-hidden />
                    )}
                  </button>
                </div>
                <div className={cn('space-y-1', !showMobileBreakdown && 'hidden lg:block')}>
                  <div className="flex justify-between tabular-nums">
                    <span>{t('cart.net_total')}</span>
                    <span>{formatCurrency(orderPreview.net_total, activeCurrency)}</span>
                  </div>
                  {orderPreview.discount_amount > 0 && (
                    <div className="flex justify-between text-emerald-700 tabular-nums">
                      <span>{t('cart.discount')}</span>
                      <span>-{formatCurrency(orderPreview.discount_amount, activeCurrency)}</span>
                    </div>
                  )}
                  {orderPreview.tax_rows.map((taxRow, idx) => (
                    <div
                      key={`${taxRow.description}-${idx}`}
                      className="flex justify-between tabular-nums"
                    >
                      <span className="truncate pe-2">{taxRow.description || t('cart.tax')}</span>
                      <span className="shrink-0">{formatCurrency(taxRow.tax_amount, activeCurrency)}</span>
                    </div>
                  ))}
                </div>
                {orderPreview.efris_warnings && orderPreview.efris_warnings.length > 0 && (
                  <div
                    data-testid="efris-readiness-alert"
                    role="alert"
                    className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900"
                  >
                    <div className="flex items-start gap-1.5">
                      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-700" aria-hidden />
                      <div className="space-y-0.5">
                        {orderPreview.efris_warnings.map((w, idx) => (
                          <p key={`${w.item_code}-${idx}`}>{w.message}</p>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ) : previewLoading ? (
              <p className="mb-2 text-xs text-gray-400">{t('cart.tax_preview_loading')}</p>
            ) : null}
            <div className="flex justify-between items-center mb-4">
              <div className="flex items-center gap-2">
                <Button
                  onClick={() => setShowCommentDialog(true)}
                  variant="ghost"
                  size="sm"
                  className={cn(
                    "h-8 w-8 p-0",
                    orderComment ? "text-blue-600" : "text-gray-500 hover:text-gray-700"
                  )}
                  disabled={isInteractionDisabled}
                  title={orderComment ? t('cart.edit_comment') : t('cart.add_comment')}
                >
                  <MessageSquare className="w-4 h-4" />
                </Button>
                <span className="text-lg font-semibold">{t('cart.total')}</span>
              </div>
              <span className="text-lg font-semibold tabular-nums">{formatCurrency(displayTotal, activeCurrency)}</span>
            </div>
            <Button
              onClick={handleSubmit}
              variant="default"
              size="default"
              className="w-full"
              disabled={isInteractionDisabled}
            >
              {isSubmitting ? (
                <div className="flex items-center">
                  <Loader2 className="w-4 h-4 me-2 animate-spin" />

                  {isUpdatingOrder ? t('cart.updating_order') : t('cart.processing_order')}
                </div>
              ) : isUpdatingOrder ? (
                t('cart.update_order')
              ) : (
                t('cart.add_new_order')
              )}
            </Button>
          </div>
        </>
      )}

      {editingItem && (
        <ProductDialog
          onClose={() => {
            setEditingItem(null);
            setSelectedItem(null);
          }}
          editMode
          initialVariant={editingItem.selectedVariant}
          initialAddons={editingItem.selectedAddons}
          initialQuantity={editingItem.quantity}
          itemToReplace={editingItem}
        />
      )}

      <CommentDialog
        isOpen={showCommentDialog}
        onClose={() => setShowCommentDialog(false)}
        onSave={handleCommentSave}
        initialComment={orderComment}
      />

      {pendingSyncVoidApproval && orderId && posProfile && (
        <ManagerApproval
          open
          posProfile={posProfile.name}
          privilege="void_sent_item"
          title={t('manager_approval.void_sent_item_title')}
          summaryLines={[
            t('manager_approval.void_sent_order_summary', { count: pendingSyncVoidApproval.linesCount || 1 }),
            t('manager_approval.amount_line', { amount: formatCurrency(pendingSyncVoidApproval.amount, activeCurrency) }),
            t('manager_approval.check_line', { check: orderId }),
          ]}
          reference={{ doctype: 'POS Invoice', name: orderId }}
          claim={pendingSyncVoidApproval.claim}
          amount={pendingSyncVoidApproval.amount}
          onApproved={(res) => void handleSyncVoidApproved(res)}
          onCancel={() => setPendingSyncVoidApproval(null)}
        />
      )}
    </div>
  );
};

export default OrderPanel;
