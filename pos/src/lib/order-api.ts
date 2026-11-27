import { assertOfflineActionAllowed } from '../offline/cashier';
import { call } from '@ury/core';
import { t } from '../i18n';
import { gatewayStatusOf } from '../offline/gateway';

export interface POSInvoiceItem {
  ury_held?: number;
  fire_at?: string | null;
  fire_confirmation_pending?: number;
  custom_course?: string;
  custom_seat_no?: number;
  name: string;
  item_code: string;
  item_name: string;
  description: string;
  item_group: string;
  image: string;
  qty: number;
  comment: string;
  /**
   * Stable per-line identity persisted on the invoice row (custom field
   * `POS Invoice Item-reservation_line_key`). Echoed back unchanged in the
   * next `sync_order` payload so the server diffs previous-vs-current lines
   * by identity instead of by child-row name (which is regenerated on every
   * save) — see `ury_order.py::_previous_line_snapshot`.
   */
  reservation_line_key?: string | null;
  custom_parent_line_key?: string | null;
  custom_line_role?: 'Standard' | 'Modifier' | 'Combo Header' | 'Combo Upcharge';
  custom_is_modifier?: number;
  custom_modifier_prefix?: string | null;
  custom_modifier_group?: string | null;
  custom_modifier_option?: string | null;
  custom_modifiers_json?: string | null;
  custom_bundle_overrides_json?: string | null;
  custom_inherit_parent_routing?: number;
  custom_hide_unpriced_on_receipt?: number;
  rate: number;
  amount: number;
  discount_percentage: number;
  discount_amount: number;
}

export interface POSInvoice {
  pos_profile?: string;
  currency?: string;
  name: string;
  title: string;
  customer: string;
  customer_name: string;
  mobile_number: string;
  customer_group: string;
  territory: string;
  posting_date: string;
  posting_time: string;
  order_type: string;
  restaurant_table: string;
  custom_merged_tables?: string | null;
  custom_split_group?: string | null;
  custom_restaurant_room: string;
  status: string;
  total: number;
  grand_total: number;
  rounded_total: number;
  items: POSInvoiceItem[];
  custom_merged_pos_invoice?: string | null;
  custom_merged_total?: number | null;
  waiter?: string;
  invoice_printed?: number;
  docstatus?: number;
  no_of_pax?: number;
  custom_comments?: string | null;
  ury_client_order_id?: string | null;
  custom_guest_label?: string | null;
  modified?: string;
  held_update_previous_modified?: string;
}

export interface TableOrder {
  message: POSInvoice | null;
}

/**
 * Fetches the current active order/invoice for a table if any exists
 * @param table_no The table number to fetch the order for
 * @returns The order details and customer information if an active order exists
 */
export async function getTableOrder(table_no: string, strict = false, payment = false): Promise<TableOrder> {
  try {
    const res = await call.get('ury.ury.doctype.ury_order.ury_order.get_order_invoice', { 
      table: table_no,
      ...(payment ? { is_payment: 'Printed' } : {}),
    });
    return res as TableOrder;
  } catch (error) {
    if (strict) throw error;
    console.error('Error fetching table order:', error);
    return { message: null };
  }
} 

export interface SyncOrderRequest {
  client_order_id?: string;
  round_id?: string;
  table?: string;
  customer?: string;
  items: Array<{
    name?: string;
    ury_held?: number;
    custom_course?: string;
    custom_seat_no?: number;
    item: string;
    item_name: string;
    rate: number;
    qty: number;
    comment?: string;
    /**
     * Stable line identity (the cart's `uniqueId`). The server accepts it
     * under any of `ury_order_reservation_service.LINE_REF_FIELDS`; we send
     * the canonical name. Omitting it makes the server fall back to
     * context/occurrence matching, which cannot survive a comment edit.
     */
    reservation_line_key?: string;
    custom_parent_line_key?: string;
    custom_line_role?: 'Standard' | 'Modifier' | 'Combo Header' | 'Combo Upcharge';
    custom_is_modifier?: number;
    custom_modifier_prefix?: string;
    custom_modifier_group?: string;
    custom_modifier_option?: string;
    custom_modifiers_json?: string;
    custom_bundle_overrides_json?: string;
    custom_inherit_parent_routing?: number;
    custom_hide_unpriced_on_receipt?: number;
  }>;
  no_of_pax: number;
  mode_of_payment?: string;
  cashier?: string;
  owner?: string;
  waiter?: string;
  pos_profile: string;
  invoice: string | null;
  aggregator_id?: string | null;
  order_type: string;
  last_invoice: string | null;
  last_modified_time?: string;
  comments?: string | null;
  /** absent keeps the order's label; "" clears it */
  guest_label?: string;
  room?: string;
  approval_token?: string;
  void_reason?: string;
  pickup_approval_token?: string;
  guest_count_approval_token?: string;
  order_type_approval_token?: string;
  privilege_reason?: string;
  privilege_reason_note?: string;
}

/**
 * sync_order returns the synced POS Invoice on success, or `{ status: 'Failure' }`
 * when the backend rejects the write (e.g. stale last_modified_time, table already
 * occupied, or an already-billed invoice).
 */
export type SavedInvoice = POSInvoice & import('./invoice-api').POSInvoice;
export type SyncOrderResponse = SavedInvoice | { status: 'Failure' };

async function postHeldOrder<T>(method: string, params: object) {
  const action = t('hold_fire.action');
  await assertOfflineActionAllowed(action);
  try {
    return await call.post<{ message: T }>(method, params);
  } catch (failure) {
    const httpStatus = gatewayStatusOf(failure);
    if (!httpStatus) throw failure;
    throw Object.assign(new Error(t('offline.refused', { action })), { httpStatus, cause: failure });
  }
}

export const syncOrder = async (data: SyncOrderRequest) => {
  const method = 'ury.ury.doctype.ury_order.ury_order.sync_order';
  if (data.items?.some(item => item.ury_held)) {
    return postHeldOrder<SyncOrderResponse>(method, data);
  }
  await assertOfflineActionAllowed('online-order rounds');
  return call.post<{ message: SyncOrderResponse }>(method, data);
};

export interface HeldSelection {
  line?: string;
  course?: string;
  last_modified_time?: string;
}

export function fireItems(invoice: string, selection: HeldSelection) {
  return postHeldOrder<POSInvoice>(
    'ury.ury.doctype.ury_order.ury_order.fire_items', { invoice, ...selection },
  );
}

export function scheduleItems(invoice: string, selection: HeldSelection & { delay_minutes: number }) {
  return postHeldOrder<POSInvoice>(
    'ury.ury.doctype.ury_order.ury_order.schedule_held_items', { invoice, ...selection },
  );
}

export interface SplitBillItemMove {
  name: string;
  qty: number;
}

export interface SplitBillResponse {
  source_invoice: string;
  new_invoice: string;
}

export interface OrderActionAuth {
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}

export async function splitBill(
  sourceInvoice: string,
  itemsToMove: SplitBillItemMove[],
  customer?: string | null,
  guestLabel?: string | null,
  auth?: OrderActionAuth
): Promise<SplitBillResponse> {
  await assertOfflineActionAllowed('split');
  const res = await call.post('ury.ury.doctype.ury_order.ury_order.split_bill', {
    source_invoice: sourceInvoice,
    items_to_move: itemsToMove,
    customer: customer || undefined,
    ...(guestLabel && guestLabel.trim() ? { guest_label: guestLabel.trim() } : {}),
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
  return res.message as SplitBillResponse;
}

export async function splitCheckMulti(
  sourceInvoice: string,
  payload: import('../components/BillSplitDialog').MultiSplitRequest,
  auth?: OrderActionAuth,
): Promise<{ source_invoice: string; new_invoices: string[] }> {
  await assertOfflineActionAllowed('split');
  const res = await call.post('ury.ury.doctype.ury_order.ury_order.split_check_multi', {
    source_invoice: sourceInvoice,
    mode: payload.mode,
    ...('splits' in payload ? { splits: payload.splits } : {}),
    ...('splitCount' in payload ? { split_count: payload.splitCount } : {}),
    approval_token: auth?.approvalToken,
    reason: auth?.reason,
    reason_note: auth?.reasonNote,
  });
  return res.message;
}

export async function tableTransfer(
  table: string,
  newTable: string,
  invoice: string,
  auth?: OrderActionAuth,
  targetProfile?: string
): Promise<void> {
  await assertOfflineActionAllowed('transfer');
  await call.post('ury.ury.doctype.ury_order.ury_order.table_transfer', {
    join_roster: 1,
    ...(targetProfile ? { target_pos_profile: targetProfile } : {}),
    table,
    newTable,
    invoice,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
}

export async function getCaptainTransferCandidates(invoice: string) {
  const result = await call.get<{ message: { name: string; full_name?: string }[] }>(
    'ury.ury.doctype.ury_order.ury_order.get_captain_transfer_candidates', { invoice },
  );
  return result.message;
}

export async function captainTransfer(
  currentCaptain: string,
  newCaptain: string,
  invoice: string,
  auth?: OrderActionAuth
): Promise<void> {
  await assertOfflineActionAllowed('transfer');
  await call.post('ury.ury.doctype.ury_order.ury_order.captain_transfer', {
    currentCaptain,
    newCaptain,
    invoice,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
}

/**
 * Reprints / re-fires the KOT for an invoice via `reprint_kot`,
 * supporting `reprint_kot` privilege rules, reason, and Manager PIN in place.
 */
export async function reprintKot(invoiceNumber: string, auth?: OrderActionAuth): Promise<void> {
  await assertOfflineActionAllowed('kitchen re-fire');
  await call.post('ury.ury.api.ury_kot_reprint.reprint_kot', {
    invoice_number: invoiceNumber,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
}

export async function cancelOrder(
  invoice_id: string,
  reason: string
): Promise<void> {
  await assertOfflineActionAllowed('void');
  await call.post('ury.ury.doctype.ury_order.ury_order.cancel_order', {
    invoice_id,
    reason,
  });
}

export interface ReduceOrderItemQtyResponse {
  invoice: string;
  item_row_name: string;
  item_code: string;
  previous_qty: number;
  new_qty: number;
  delta: number;
  order_type: string;
  cancel_kot_names: string[];
  actor: string;
  approved_by?: string | null;
}

/**
 * Reduces (or fully removes, `newQty = 0`) one item's qty on an already
 * saved/printed POS Invoice via `ury.ury.api.ury_pos_invoice_qty_reduction
 * .reduce_order_item_qty` — generic across cashier (Register) and captain
 * callers by design (see that module's docstring). Only permitted when the
 * invoice's `order_type` is in the POS Profile's
 * `custom_qty_reduction_allowed_order_types` allow-list; the server raises
 * a distinct `ORDER_TYPE_NOT_ALLOWED` error (message text only — no
 * structured reason code in the response) otherwise, which callers should
 * detect from the message and surface as a clear per-order-type rejection
 * rather than a generic failure toast.
 *
 * `itemRowName` must be the actual POS Invoice Item child-table row `name`
 * (NOT `item_code`) — the server matches and removes exactly that row.
 * Matching by item_code alone was ambiguous whenever an item appeared on
 * more than one row.
 */
export async function reduceOrderItemQty(
  invoiceId: string,
  itemRowName: string,
  newQty: number,
  reason?: string,
  approvalToken?: string
): Promise<ReduceOrderItemQtyResponse> {
  await assertOfflineActionAllowed('quantity reduction');
  const payload: Record<string, unknown> = {
    invoice_id: invoiceId,
    item_row_name: itemRowName,
    new_qty: newQty,
    reason,
  };
  if (approvalToken) {
    payload.approval_token = approvalToken;
  }
  const res = await call.post<{ message: ReduceOrderItemQtyResponse }>(
    'ury.ury.api.ury_pos_invoice_qty_reduction.reduce_order_item_qty',
    payload
  );
  return res.message;
}

/**
 * True when a caught error from `reduceOrderItemQty` is the server's
 * `ORDER_TYPE_NOT_ALLOWED` rejection (detected from the message text — the
 * backend does not expose a structured reason code in the response body).
 */
export function isOrderTypeNotAllowedError(message: string | null | undefined): boolean {
  if (!message) return false;
  return /not permitted for order type/i.test(message);
}

/**
 * True when a caught error from `reduceOrderItemQty` is the server's
 * `LAST_ITEM_CANNOT_BE_REMOVED` rejection (detected from the message text —
 * the backend does not expose a structured reason code in the response
 * body). Callers should surface this distinctly, telling the user to cancel
 * the whole invoice instead of removing its last item.
 */
export function isLastItemCannotBeRemovedError(message: string | null | undefined): boolean {
  if (!message) return false;
  return /only item on invoice/i.test(message);
}

export { formatPreviewTaxLabel } from './tax-preview-label';

export interface PreviewTaxRow {
  description: string;
  rate: number;
  header_rate?: number;
  effective_rate?: number | null;
  item_rate_override?: boolean;
  tax_amount: number;
  account_head?: string;
  custom_pos_charge_kind?: string;
}

export interface EfrisWarning {
  code: string;
  item_code: string;
  item_name?: string;
  missing?: string[];
  message: string;
}

export interface OrderTotalsPreview {
  subtotal?: number;
  net_total: number;
  discount_amount: number;
  tax_rows: PreviewTaxRow[];
  total_taxes_and_charges?: number;
  grand_total: number;
  rounded_total: number;
  rounding_adjustment?: number;
  tax_category?: string;
  taxes_and_charges?: string;
  serving_period?: string | null;
  transaction_local_time?: string | null;
  transaction_weekday?: string | null;
  item_taxes: Array<{
    item_code: string;
    item_tax_template?: string | null;
    item_tax_rate?: string;
    rate?: number;
    net_amount?: number;
  }>;
  efris_warnings: EfrisWarning[];
}

export interface PreviewOrderTotalsRequest {
  pos_profile: string;
  order_type: string;
  customer?: string;
  items?: Array<{
    name?: string;
    item: string;
    qty: number;
    rate?: number;
    item_name?: string;
    reservation_line_key?: string;
    custom_parent_line_key?: string;
    custom_line_role?: 'Standard' | 'Modifier' | 'Combo Header' | 'Combo Upcharge';
    custom_is_modifier?: number;
    custom_modifier_prefix?: string;
    custom_modifier_group?: string;
    custom_modifier_option?: string;
    custom_modifiers_json?: string;
    custom_bundle_overrides_json?: string;
    custom_inherit_parent_routing?: number;
    custom_hide_unpriced_on_receipt?: number;
  }>;
  no_of_pax?: number;
  invoice_name?: string | null;
  additional_discount_percentage?: number;
  coupon_code?: string;
  room?: string;
  table?: string;
}

export async function previewOrderTotals(
  payload: PreviewOrderTotalsRequest,
): Promise<OrderTotalsPreview> {
  const res = await call.post<{ message: OrderTotalsPreview }>(
    'ury.ury_pos.api.preview_order_totals',
    payload,
  );
  return res.message;
}

export async function changeOrderType(
  invoice: string,
  orderType: string,
  opts?: { table?: string; room?: string; noOfPax?: number; reason?: string },
): Promise<POSInvoice> {
  await assertOfflineActionAllowed('order type change');
  const res = await call.post<{ message: POSInvoice }>(
    'ury.ury.doctype.ury_order.ury_order.change_order_type',
    {
      invoice,
      order_type: orderType,
      ...(opts?.table ? { table: opts.table } : {}),
      ...(opts?.room ? { room: opts.room } : {}),
      ...(opts?.noOfPax ? { no_of_pax: opts.noOfPax } : {}),
      ...(opts?.reason ? { reason: opts.reason } : {}),
    },
  );
  return res.message;
}