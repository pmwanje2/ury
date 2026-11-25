import { useEffect, useState } from 'react';
import { Trash2, Edit, FrownIcon, Plus, Loader2, MessageSquare, ChevronDown, ChevronUp } from 'lucide-react';
import { usePOSStore } from '../store/pos-store';
import { cn } from '@ury/ui';
import { formatCurrency, parseFrappeError, resolveDefaultCustomer } from '@ury/core';
import { CustomerSelect } from './CustomerSelect';
import ProductDialog from './ProductDialog';
import OrderTypeSelect from './OrderTypeSelect';
import CommentDialog from './CommentDialog';
import CheckPrivilegedDialog from './CheckPrivilegedDialog';
import { Button, Badge } from '@ury/ui';
import { getOrderKotStatus, type KotItemState } from '../lib/kot-status-api';
import { Spinner } from '@ury/ui';
import { HoldFireControls } from './HoldFireControls';
import { useOutbox } from '../offline/use-outbox';
import { roundItems } from '../offline/outbox';
import { submitOrderOfflineAware } from '../offline/cashier';
import { syncOrder, type SavedInvoice, type SyncOrderRequest } from '../lib/order-api';
import { isAuthRequiredError, overrideErrorMessage } from '../lib/override-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { useRootStore } from '../store/root-store';
import type { RootState } from '../store/root-store';
import { showToast } from '@ury/ui';
import { DINE_IN, orderTypeLabel } from '../data/order-types';
import { t } from '../i18n';
import { cartQtyForItem, remainingHeadroom } from '../lib/cart-capacity';
import type { MenuAvailability } from '../lib/availability-api';
import {
  estimateServiceCharge,
  resolveActiveServiceChargeRule,
  roundCurrency,
} from '../lib/service-charge';

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
  const [mobileSummaryOpen, setMobileSummaryOpen] = useState(false);
  const [showServiceChargeDialog, setShowServiceChargeDialog] = useState(false);
  const [pendingSyncVoidApproval, setPendingSyncVoidApproval] = useState<{
    privilege: 'void_sent_item' | 'guest_count_change' | 'order_type_change' | 'pickup_other_check';
    orderData: SyncOrderRequest;
    amount: number;
    linesCount: number;
    claim: Record<string, unknown>;
    summaryLines: string[];
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
  const activeServiceChargeRule = resolveActiveServiceChargeRule(posProfile, selectedOrderType, noOfPax);
  const estimatedServiceCharge = estimateServiceCharge(total, activeServiceChargeRule, activeCurrency);
  const estimatedTotal = roundCurrency(total + estimatedServiceCharge, activeCurrency);
  const serviceChargeMeta = activeServiceChargeRule
    ? [
        activeServiceChargeRule.charge_mode === 'Fixed Amount'
          ? t('privileged_actions.sc_fixed_label')
          : `${activeServiceChargeRule.rate || 0}%`,
        activeServiceChargeRule.order_type === '*' ? null : orderTypeLabel(activeServiceChargeRule.order_type),
        (activeServiceChargeRule.min_pax || 0) > 0
          ? t('privileged_actions.sc_pax_threshold', { count: activeServiceChargeRule.min_pax })
          : null,
      ]
        .filter(Boolean)
        .join(' · ')
    : '';

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
        if (voidInfo.linesCount > 0) {
          setPendingSyncVoidApproval({
            privilege: 'void_sent_item',
            orderData,
            amount: voidInfo.amount,
            linesCount: voidInfo.linesCount,
            claim: voidInfo.claim,
            summaryLines: [
              t('manager_approval.void_sent_order_summary', { count: voidInfo.linesCount }),
              t('manager_approval.amount_line', { amount: formatCurrency(voidInfo.amount, currency) }),
              t('manager_approval.check_line', { check: orderId }),
            ],
          });
          return;
        }
        const existingInv = tableOrder?.message?.name === orderId ? tableOrder.message : null;
        if (existingInv?.order_type && orderData.order_type && existingInv.order_type !== orderData.order_type) {
          setPendingSyncVoidApproval({
            privilege: 'order_type_change',
            orderData,
            amount: 0,
            linesCount: 0,
            claim: {
              action: 'order_type_change',
              invoice: orderId,
              from_order_type: existingInv.order_type,
              to_order_type: orderData.order_type,
            },
            summaryLines: [
              `${existingInv.order_type} → ${orderData.order_type}`,
              t('manager_approval.check_line', { check: orderId }),
            ],
          });
          return;
        }
        if (existingInv?.no_of_pax && Number(existingInv.no_of_pax) !== Number(orderData.no_of_pax)) {
          setPendingSyncVoidApproval({
            privilege: 'guest_count_change',
            orderData,
            amount: 0,
            linesCount: 0,
            claim: {
              action: 'guest_count_change',
              invoice: orderId,
              from_pax: Number(existingInv.no_of_pax),
              to_pax: Number(orderData.no_of_pax),
            },
            summaryLines: [
              `${existingInv.no_of_pax} → ${orderData.no_of_pax}`,
              t('manager_approval.check_line', { check: orderId }),
            ],
          });
          return;
        }
        if (existingInv?.waiter && existingInv.waiter !== user.name) {
          setPendingSyncVoidApproval({
            privilege: 'pickup_other_check',
            orderData,
            amount: 0,
            linesCount: 0,
            claim: {
              action: 'pickup_other_check',
              invoice: orderId,
              owner_waiter: existingInv.waiter,
            },
            summaryLines: [
              `${t('privileged_actions.pickup_other_check')}: ${existingInv.waiter}`,
              t('manager_approval.check_line', { check: orderId }),
            ],
          });
          return;
        }
        setPendingSyncVoidApproval({
          privilege: 'void_sent_item',
          orderData,
          amount: voidInfo.amount,
          linesCount: voidInfo.linesCount,
          claim: voidInfo.claim,
          summaryLines: [
            t('manager_approval.void_sent_order_summary', { count: voidInfo.linesCount || 1 }),
            t('manager_approval.amount_line', { amount: formatCurrency(voidInfo.amount, currency) }),
            t('manager_approval.check_line', { check: orderId }),
          ],
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
    const tokenPatch: Partial<SyncOrderRequest> =
      pending.privilege === 'guest_count_change'
        ? { guest_count_approval_token: res.token, privilege_reason: res.reason, privilege_reason_note: res.note }
        : pending.privilege === 'order_type_change'
          ? { order_type_approval_token: res.token, privilege_reason: res.reason, privilege_reason_note: res.note }
          : pending.privilege === 'pickup_other_check'
            ? { pickup_approval_token: res.token, privilege_reason: res.reason, privilege_reason_note: res.note }
            : { approval_token: res.token, void_reason: res.reason };
    await submitSyncPayload(
      {
        ...pending.orderData,
        ...tokenPatch,
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
          ...(item.parentLineKey ? { custom_parent_line_key: item.parentLineKey } : {}),
          ...(item.lineRole ? { custom_line_role: item.lineRole } : {}),
          ...(item.isModifier ? { custom_is_modifier: 1 } : {}),
          ...(item.modifierPrefix ? { custom_modifier_prefix: item.modifierPrefix } : {}),
          ...(item.modifierGroup ? { custom_modifier_group: item.modifierGroup } : {}),
          ...(item.modifierOption ? { custom_modifier_option: item.modifierOption } : {}),
          ...(item.selectedModifiers && item.selectedModifiers.length > 0
            ? { custom_modifiers_json: JSON.stringify(item.selectedModifiers) }
            : {}),
          ...(item.bundleOverrides && item.bundleOverrides.length > 0
            ? { custom_bundle_overrides_json: JSON.stringify(item.bundleOverrides) }
            : {}),
          ...(item.inheritParentRouting !== undefined
            ? { custom_inherit_parent_routing: item.inheritParentRouting ? 1 : 0 }
            : {}),
          ...(item.hideUnpricedOnReceipt ? { custom_hide_unpriced_on_receipt: 1 } : {}),
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
            {activeOrders.map((item) => {
              if (item.isModifier) {
                return (
                  <div
                    key={item.uniqueId}
                    data-testid={`modifier-child-${item.uniqueId}`}
                    className={cn(
                      'ms-4 flex items-center justify-between border-b border-s-2 border-gray-100 border-s-blue-200 bg-gray-50/70 py-2 ps-3 pe-1 text-xs',
                      isInteractionDisabled && 'opacity-50'
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="font-medium text-gray-700 truncate">
                        + {item.modifierPrefix ? `${item.modifierPrefix} ` : ''}
                        {item.name}
                      </p>
                      <p className="text-gray-500">
                        {item.quantity} × {formatCurrency(item.price, activeCurrency)}
                      </p>
                    </div>
                    <span className="ms-2 shrink-0 font-medium text-gray-700">
                      {formatCurrency(calculateItemTotal(item), activeCurrency)}
                    </span>
                  </div>
                );
              }

              return (
                <div
                  key={item.uniqueId}
                  className={cn(
                    "flex flex-col py-4 border-b border-gray-100",
                    isInteractionDisabled && "opacity-50"
                  )}
                >
                  <div className="flex items-center justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
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
                      {item.selectedModifiers && item.selectedModifiers.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {item.selectedModifiers.map((mod) => (
                            <span
                              key={`${mod.group}-${mod.option_code}-${mod.prefix || ''}`}
                              className="inline-flex items-center rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-800"
                            >
                              {mod.prefix ? `${mod.prefix} ` : ''}
                              {mod.label}
                            </span>
                          ))}
                        </div>
                      )}
                      {item.bundleOverrides && item.bundleOverrides.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {item.bundleOverrides.map((ov) => (
                            <span
                              key={`${ov.group}-${ov.chosen_item}`}
                              className="inline-flex items-center rounded bg-emerald-50 px-1.5 py-0.5 text-xs text-emerald-800"
                            >
                              {ov.label}
                            </span>
                          ))}
                        </div>
                      )}
                      <p className="text-gray-600 text-sm">{formatCurrency(calculateItemTotal(item), activeCurrency)}</p>
                    </div>
                    
                    <div className="flex items-center gap-2">
                      <Button
                        onClick={() => handleEdit(item)}
                        variant="ghost"
                        size="icon"
                        className="text-blue-600 hover:text-blue-700"
                        title={item.has_modifiers || item.is_combo_meal ? t('cart.modify') : t('cart.edit_item')}
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
              );
            })}
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
          
          <div className="sticky bottom-0 p-4 border-t border-border flex-shrink-0 bg-white lg:static">
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
            {/* Phone (390px) collapsible summary bar with >=44px touch target */}
            <div className="lg:hidden mb-3">
              <button
                type="button"
                data-testid="mobile-summary-toggle"
                aria-expanded={mobileSummaryOpen}
                onClick={() => setMobileSummaryOpen((prev) => !prev)}
                className="min-h-11 w-full flex items-center justify-between gap-2 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-start text-sm"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-gray-600">
                    {t('privileged_actions.sc_summary_net')}: {formatCurrency(total, activeCurrency)}
                    {activeServiceChargeRule && estimatedServiceCharge > 0
                      ? ` · ${activeServiceChargeRule.description || t('privileged_actions.service_charge')}: ${formatCurrency(estimatedServiceCharge, activeCurrency)}`
                      : ''}
                  </div>
                  <div className="text-xs font-medium text-blue-700">
                    {t('privileged_actions.sc_summary_toggle')}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1.5 font-semibold text-gray-900 tabular-nums">
                  <span>{formatCurrency(estimatedTotal, activeCurrency)}</span>
                  {mobileSummaryOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                </div>
              </button>
              {mobileSummaryOpen && (
                <div className="mt-2 rounded-md border border-gray-200 bg-white p-3 space-y-1.5 text-sm">
                  <div className="flex items-center justify-between text-gray-600">
                    <span>{t('privileged_actions.sc_summary_net')}</span>
                    <span className="tabular-nums">{formatCurrency(total, activeCurrency)}</span>
                  </div>
                  {activeServiceChargeRule && estimatedServiceCharge > 0 && (
                    <div className="flex items-center justify-between gap-2 text-gray-700">
                      <span className="min-w-0 truncate">
                        {activeServiceChargeRule.description || t('privileged_actions.service_charge')}
                        {serviceChargeMeta ? ` (${serviceChargeMeta})` : ''}
                      </span>
                      <span className="shrink-0 tabular-nums font-medium">
                        {formatCurrency(estimatedServiceCharge, activeCurrency)}
                      </span>
                    </div>
                  )}
                  <div className="flex items-center justify-between border-t border-gray-100 pt-1.5 font-semibold text-gray-900">
                    <span>{t('cart.total')}</span>
                    <span className="tabular-nums">{formatCurrency(estimatedTotal, activeCurrency)}</span>
                  </div>
                </div>
              )}
            </div>

            {/* Desktop summary breakdown */}
            <div className="hidden lg:block space-y-1.5 mb-3">
              {activeServiceChargeRule && estimatedServiceCharge > 0 && (
                <>
                  <div className="flex items-center justify-between text-sm text-gray-600">
                    <span>{t('privileged_actions.sc_summary_net')}</span>
                    <span className="tabular-nums">{formatCurrency(total, activeCurrency)}</span>
                  </div>
                  <div
                    data-testid="order-panel-sc-row"
                    className="flex items-center justify-between gap-2 text-sm text-gray-700"
                  >
                    {orderId ? (
                      <button
                        type="button"
                        onClick={() => setShowServiceChargeDialog(true)}
                        className="min-w-0 truncate text-start text-blue-700 hover:underline"
                      >
                        {activeServiceChargeRule.description || t('privileged_actions.service_charge')}
                        {serviceChargeMeta ? ` (${serviceChargeMeta})` : ''}
                      </button>
                    ) : (
                      <span className="min-w-0 truncate">
                        {activeServiceChargeRule.description || t('privileged_actions.service_charge')}
                        {serviceChargeMeta ? ` (${serviceChargeMeta})` : ''}
                      </span>
                    )}
                    <span className="shrink-0 tabular-nums font-medium">
                      {formatCurrency(estimatedServiceCharge, activeCurrency)}
                    </span>
                  </div>
                </>
              )}
            </div>

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
              <span className="text-lg font-semibold tabular-nums">{formatCurrency(estimatedTotal, activeCurrency)}</span>
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

      {showServiceChargeDialog && orderId && posProfile && (
        <CheckPrivilegedDialog
          open
          onOpenChange={setShowServiceChargeDialog}
          initialTab="service_charge"
          posProfile={posProfile.name}
          company={posProfile.company || ''}
          invoiceName={orderId}
          currency={activeCurrency || undefined}
          onSuccess={() => setShowServiceChargeDialog(false)}
        />
      )}

      {pendingSyncVoidApproval && orderId && posProfile && (
        <ManagerApproval
          open
          posProfile={posProfile.name}
          privilege={pendingSyncVoidApproval.privilege}
          title={
            pendingSyncVoidApproval.privilege === 'void_sent_item'
              ? t('manager_approval.void_sent_item_title')
              : t(`privileged_actions.${pendingSyncVoidApproval.privilege}`)
          }
          summaryLines={pendingSyncVoidApproval.summaryLines}
          reference={{ doctype: 'POS Invoice', name: orderId }}
          claim={pendingSyncVoidApproval.claim}
          amount={pendingSyncVoidApproval.amount || undefined}
          onApproved={(res) => void handleSyncVoidApproved(res)}
          onCancel={() => setPendingSyncVoidApproval(null)}
        />
      )}
    </div>
  );
};

export default OrderPanel;
