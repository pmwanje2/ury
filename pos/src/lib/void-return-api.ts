import { call } from '@ury/core';
import { assertOfflineActionAllowed } from '../offline/cashier';
import type { OverrideReason } from './override-api';

const VOID_RETURN_MODULE = 'ury.ury_pos.void_return_reopen.';

export interface ReturnableCheckItem {
  item_row_name: string;
  item_code: string;
  item_name: string;
  sold_qty: number;
  returned_qty: number;
  remaining_qty: number;
  rate: number;
  amount: number;
}

export interface RefundPaymentEntry {
  mode_of_payment: string;
  amount: number;
}

export interface ReturnableCheckResponse {
  invoice: string;
  pos_profile: string;
  branch: string;
  customer: string;
  currency: string;
  precision: number;
  grand_total: number;
  discount_amount: number;
  total_taxes_and_charges: number;
  net_paid_amount: number;
  already_refunded_amount: number;
  remaining_refundable_amount: number;
  original_payments: RefundPaymentEntry[];
  available_modes: string[];
  is_room_charge: boolean;
  room_number?: string | null;
  is_fiscalised: boolean;
  items: ReturnableCheckItem[];
  reasons: OverrideReason[];
}

export interface ReturnPreviewResponse {
  original_invoice: string;
  currency: string;
  precision: number;
  items: Array<{
    item_row_name: string;
    item_code: string;
    item_name: string;
    qty: number;
    rate: number;
    gross: number;
  }>;
  subtotal: number;
  discount_amount: number;
  tax_amount: number;
  refund_total: number;
  max_cash_refundable: number;
  unpaid_credit_reduction: number;
  default_refund_payments: RefundPaymentEntry[];
  refund_payments: RefundPaymentEntry[];
  is_room_charge: boolean;
  is_fiscalised: boolean;
}

export interface CreatePosReturnResponse {
  return_invoice: string;
  original_invoice: string;
  currency: string;
  subtotal?: number;
  discount_amount?: number;
  tax_amount?: number;
  refund_total: number;
  max_cash_refundable?: number;
  unpaid_credit_reduction?: number;
  refund_payments: RefundPaymentEntry[];
  approver: string;
  reason: string;
  is_room_charge?: boolean;
  is_fiscalised?: boolean;
  efris_creditnote_reasoncode?: string | null;
}

export interface AdjustClosedCheckResponse {
  action: 'adjust_closed_check';
  original_invoice: string;
  reversal_return: string;
  replacement_invoice: string;
  approver: string;
  reason?: string;
  credit_and_rebill: boolean;
}

export interface ReopenClosedCheckResponse {
  action: 'reopen_closed_check';
  original_invoice: string;
  reversal_return: string;
  reopened_invoice: string;
  table_restored: boolean;
  restaurant_table?: string | null;
  approver: string;
  reason?: string;
  credit_and_rebill: boolean;
}

export interface ReduceItemWithWasteResponse {
  invoice: string;
  item_row_name: string;
  item_code: string;
  previous_qty: number;
  new_qty: number;
  delta: number;
  cancel_kot_names: string[];
  approver?: string | null;
  approved_by?: string | null;
  reason?: string;
  is_waste?: boolean;
  waste_stock_entry?: string | null;
}

export async function reduceSentItemWithWaste(params: {
  invoiceId: string;
  itemRowName: string;
  newQty: number;
  reason: string;
  note?: string;
  isWaste?: boolean;
  approvalToken?: string;
}): Promise<ReduceItemWithWasteResponse> {
  await assertOfflineActionAllowed('sent item void');
  const payload: Record<string, unknown> = {
    invoice_id: params.invoiceId,
    item_row_name: params.itemRowName,
    new_qty: params.newQty,
    reason: params.reason,
    is_waste: params.isWaste ? 1 : 0,
  };
  if (params.note) payload.note = params.note;
  if (params.approvalToken) payload.approval_token = params.approvalToken;
  const res = await call.post<{ message: ReduceItemWithWasteResponse }>(
    'ury.ury.api.ury_pos_invoice_qty_reduction.reduce_order_item_qty',
    payload,
  );
  return res.message;
}

export async function cancelCheckWithApproval(params: {
  invoiceId: string;
  reason: string;
  note?: string;
  isWaste?: boolean;
  approvalToken?: string;
}): Promise<Record<string, unknown>> {
  await assertOfflineActionAllowed('order cancellation');
  const payload: Record<string, unknown> = {
    invoice_id: params.invoiceId,
    reason: params.reason,
  };
  if (params.note) payload.note = params.note;
  if (params.isWaste) payload.is_waste = 1;
  if (params.approvalToken) payload.approval_token = params.approvalToken;
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury.doctype.ury_order.ury_order.cancel_order',
    payload,
  );
  return res.message;
}

export async function getReturnableCheck(invoiceId: string): Promise<ReturnableCheckResponse> {
  await assertOfflineActionAllowed('check return');
  const res = await call.get<{ message: ReturnableCheckResponse }>(
    VOID_RETURN_MODULE + 'get_returnable_check',
    { invoice_id: invoiceId },
  );
  return res.message;
}

export async function previewPosReturn(params: {
  originalInvoice: string;
  items: Array<{ item_row_name: string; qty: number }>;
  refundPayments?: RefundPaymentEntry[];
  allowDifferentTender?: boolean;
}): Promise<ReturnPreviewResponse> {
  const query: Record<string, unknown> = {
    original_invoice: params.originalInvoice,
    items: JSON.stringify(params.items),
    allow_different_tender: params.allowDifferentTender ? 1 : 0,
  };
  if (params.refundPayments) {
    query.refund_payments = JSON.stringify(params.refundPayments);
  }
  const res = await call.get<{ message: ReturnPreviewResponse }>(
    VOID_RETURN_MODULE + 'preview_pos_return',
    query,
  );
  return res.message;
}

export async function createPosReturn(params: {
  originalInvoice: string;
  items: Array<{ item_row_name: string; qty: number }>;
  reason: string;
  note?: string;
  refundPayments?: RefundPaymentEntry[];
  allowDifferentTender?: boolean;
  approvalToken?: string;
  efrisReasonCode?: string;
}): Promise<CreatePosReturnResponse> {
  await assertOfflineActionAllowed('return or refund');
  const payload: Record<string, unknown> = {
    original_invoice: params.originalInvoice,
    items: params.items,
    reason: params.reason,
    allow_different_tender: params.allowDifferentTender ? 1 : 0,
  };
  if (params.note) payload.note = params.note;
  if (params.refundPayments) payload.refund_payments = params.refundPayments;
  if (params.approvalToken) payload.approval_token = params.approvalToken;
  if (params.efrisReasonCode) payload.efris_reason_code = params.efrisReasonCode;
  const res = await call.post<{ message: CreatePosReturnResponse }>(
    VOID_RETURN_MODULE + 'create_pos_return',
    payload,
  );
  return res.message;
}

export async function adjustClosedCheck(params: {
  invoiceId: string;
  payments: RefundPaymentEntry[];
  reason: string;
  note?: string;
  tipAmount?: number;
  creditAndRebill?: boolean;
  approvalToken?: string;
}): Promise<AdjustClosedCheckResponse> {
  await assertOfflineActionAllowed('closed check adjustment');
  const payload: Record<string, unknown> = {
    invoice_id: params.invoiceId,
    payments: params.payments,
    reason: params.reason,
    credit_and_rebill: params.creditAndRebill ? 1 : 0,
  };
  if (params.note) payload.note = params.note;
  if (params.tipAmount !== undefined) payload.tip_amount = params.tipAmount;
  if (params.approvalToken) payload.approval_token = params.approvalToken;
  const res = await call.post<{ message: AdjustClosedCheckResponse }>(
    VOID_RETURN_MODULE + 'adjust_closed_check',
    payload,
  );
  return res.message;
}

export async function reopenClosedCheck(params: {
  invoiceId: string;
  reason: string;
  note?: string;
  creditAndRebill?: boolean;
  approvalToken?: string;
}): Promise<ReopenClosedCheckResponse> {
  await assertOfflineActionAllowed('reopening a closed check');
  const payload: Record<string, unknown> = {
    invoice_id: params.invoiceId,
    reason: params.reason,
    credit_and_rebill: params.creditAndRebill ? 1 : 0,
  };
  if (params.note) payload.note = params.note;
  if (params.approvalToken) payload.approval_token = params.approvalToken;
  const res = await call.post<{ message: ReopenClosedCheckResponse }>(
    VOID_RETURN_MODULE + 'reopen_closed_check',
    payload,
  );
  return res.message;
}

export function formatMoneyAmount(amount: number, currency: string, precision?: number): string {
  const zeroDec = ['UGX', 'RWF', 'BIF', 'JPY', 'KRW', 'VND', 'TZS', 'XAF', 'XOF'].includes(
    (currency || '').toUpperCase(),
  );
  const digits = precision !== undefined ? precision : zeroDec ? 0 : 2;
  const formatted = Number(amount || 0).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${currency} ${formatted}`;
}
