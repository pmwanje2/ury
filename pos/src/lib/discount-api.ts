import { call } from '@ury/core';
import { assertOfflineActionAllowed } from '../offline/cashier';

export interface DiscountReasonOption {
  name: string;
  code: string;
  description: string;
  requires_supervisor: number;
  requires_note: number;
  sort_order: number;
}

export interface DiscountPresetOption {
  name: string;
  coupon_code: string;
  title: string;
  discount_percentage: number;
  discount_amount: number;
  apply_discount_on: string;
  discount_reason: string | null;
}

export interface DiscountOptionsResponse {
  reasons: DiscountReasonOption[];
  presets: DiscountPresetOption[];
  allow_discount_change: boolean;
  complimentary_enabled?: boolean;
  limit_percent: number;
  can_remove_discount: boolean;
  currency: string;
  precision: number;
}

export interface LineDiscountInput {
  row: string;
  percent?: number | null;
  amount?: number | null;
}

export interface DiscountClaim {
  percent: number | null;
  amount: number | null;
  coupon: string | null;
  lines: Array<{ row: string; percent: number | null; amount: number | null }>;
  reason: string | null;
  note: string | null;
}

export interface DiscountPreviewLine {
  row: string;
  item_code: string;
  item_name: string;
  qty: number;
  price_list_rate: number;
  rate: number;
  amount: number;
  discount_percentage: number;
  discount_amount: number;
  line_discount_total: number;
  max_discount: number;
}

export interface DiscountPreviewResponse {
  before_total: number;
  discount_total: number;
  header_discount_amount: number;
  line_discount_total: number;
  tax_total: number;
  after_total: number;
  grand_total: number;
  rounded_total: number;
  net_total: number;
  effective_percent: number;
  currency: string;
  precision: number;
  limit_percent: number;
  needs_approval: boolean;
  is_comp: boolean;
  privilege: 'discount' | 'complimentary';
  approval_reasons: string[];
  reason_required: boolean;
  note_required: boolean;
  reason: string | null;
  reason_description: string | null;
  coupon_code: string | null;
  claim: DiscountClaim;
  lines: DiscountPreviewLine[];
}

export interface CheckLineDiscountSummary {
  row: string;
  item_code: string;
  item_name: string;
  discount_percentage: number;
  discount_amount: number;
  line_discount_total: number;
  reason: string | null;
  automatic: boolean;
}

export interface CheckDiscountSummary {
  invoice: string;
  additional_discount_percentage: number;
  discount_amount: number;
  coupon_code: string | null;
  reason: string | null;
  reason_description: string | null;
  note: string | null;
  currency: string;
  precision: number;
  can_remove_discount: boolean;
  remove_claim: Record<string, unknown>;
  line_discounts: CheckLineDiscountSummary[];
}

export interface DiscountRequestPayload {
  invoice: string;
  pos_profile: string;
  discount_percent?: number | null;
  discount_amount?: number | null;
  discount_reason?: string | null;
  discount_note?: string | null;
  coupon_code?: string | null;
  line_discounts?: LineDiscountInput[] | null;
  approval?: string | null;
  complimentary?: number | boolean | null;
}

const ZERO_DECIMAL_CURRENCIES = new Set([
  'UGX', 'JPY', 'KRW', 'RWF', 'TZS', 'XAF', 'XOF', 'BIF', 'GNF', 'MGA', 'VND',
]);
const THREE_DECIMAL_CURRENCIES = new Set(['KWD', 'BHD', 'OMR', 'JOD', 'TND', 'IQD']);

export function currencyPrecisionFor(currency?: string | null, explicitPrecision?: number | null): number {
  if (typeof explicitPrecision === 'number' && Number.isInteger(explicitPrecision) && explicitPrecision >= 0) {
    return explicitPrecision;
  }
  const code = (currency || '').trim().toUpperCase();
  if (!code) return 2;
  if (ZERO_DECIMAL_CURRENCIES.has(code)) return 0;
  if (THREE_DECIMAL_CURRENCIES.has(code)) return 3;
  try {
    return (
      new Intl.NumberFormat('en', { style: 'currency', currency: code }).resolvedOptions()
        .maximumFractionDigits ?? 2
    );
  } catch {
    return 2;
  }
}

export function formatCurrencyPrecision(
  amount: number,
  currency?: string | null,
  explicitPrecision?: number | null,
): string {
  const digits = currencyPrecisionFor(currency, explicitPrecision);
  const safe = Number.isFinite(amount) ? amount : 0;
  const code = (currency || '').trim().toUpperCase();
  if (code) {
    try {
      return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: code,
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }).format(safe);
    } catch {
      return `${code} ${safe.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    }
  }
  return safe.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export async function listDiscountOptions(
  posProfile: string,
  invoice?: string,
): Promise<DiscountOptionsResponse> {
  const params: Record<string, unknown> = { pos_profile: posProfile };
  if (invoice) params.invoice = invoice;
  const { message } = await call.get<{ message: DiscountOptionsResponse }>(
    'ury.ury.doctype.ury_order.ury_order.list_discount_options',
    params,
  );
  return message;
}

export async function getCheckDiscounts(invoice: string): Promise<CheckDiscountSummary | null> {
  const { message } = await call.get<{ message: CheckDiscountSummary }>(
    'ury.ury.doctype.ury_order.ury_order.get_check_discounts',
    { invoice },
  );
  return message ?? null;
}

export async function previewCheckDiscount(
  payload: DiscountRequestPayload,
): Promise<DiscountPreviewResponse> {
  const params: Record<string, unknown> = {
    invoice: payload.invoice,
    pos_profile: payload.pos_profile,
  };
  if (payload.discount_percent !== undefined && payload.discount_percent !== null) {
    params.discount_percent = payload.discount_percent;
  }
  if (payload.discount_amount !== undefined && payload.discount_amount !== null) {
    params.discount_amount = payload.discount_amount;
  }
  if (payload.discount_reason) params.discount_reason = payload.discount_reason;
  if (payload.discount_note) params.discount_note = payload.discount_note;
  if (payload.coupon_code) params.coupon_code = payload.coupon_code;
  if (payload.complimentary) params.complimentary = 1;
  if (payload.line_discounts !== undefined && payload.line_discounts !== null) {
    params.line_discounts = JSON.stringify(payload.line_discounts);
  }
  const { message } = await call.get<{ message: DiscountPreviewResponse }>(
    'ury.ury.doctype.ury_order.ury_order.preview_settlement_totals',
    params,
  );
  return message;
}

export async function applyCheckDiscount(
  payload: DiscountRequestPayload,
): Promise<DiscountPreviewResponse> {
  await assertOfflineActionAllowed('discount');
  const body: Record<string, unknown> = {
    invoice: payload.invoice,
    pos_profile: payload.pos_profile,
  };
  if (payload.discount_percent !== undefined && payload.discount_percent !== null) {
    body.discount_percent = payload.discount_percent;
  }
  if (payload.discount_amount !== undefined && payload.discount_amount !== null) {
    body.discount_amount = payload.discount_amount;
  }
  if (payload.discount_reason) body.discount_reason = payload.discount_reason;
  if (payload.discount_note) body.discount_note = payload.discount_note;
  if (payload.coupon_code) body.coupon_code = payload.coupon_code;
  if (payload.complimentary) body.complimentary = 1;
  if (payload.line_discounts !== undefined && payload.line_discounts !== null) {
    body.line_discounts = payload.line_discounts;
  }
  if (payload.approval) body.approval = payload.approval;

  const { message } = await call.post<{ message: DiscountPreviewResponse }>(
    'ury.ury.doctype.ury_order.ury_order.apply_check_discount',
    body,
  );
  return message;
}

export async function removeCheckDiscount(params: {
  invoice: string;
  pos_profile: string;
  row?: string | null;
  approval?: string | null;
}): Promise<DiscountPreviewResponse> {
  await assertOfflineActionAllowed('discount');
  const body: Record<string, unknown> = {
    invoice: params.invoice,
    pos_profile: params.pos_profile,
  };
  if (params.row) body.row = params.row;
  if (params.approval) body.approval = params.approval;
  const { message } = await call.post<{ message: DiscountPreviewResponse }>(
    'ury.ury.doctype.ury_order.ury_order.remove_check_discount',
    body,
  );
  return message;
}
