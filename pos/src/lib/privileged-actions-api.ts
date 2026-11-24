import { call, parseFrappeError } from '@ury/core';
import { assertOfflineActionAllowed } from '../offline/cashier';

export interface PrivilegedActionAuth {
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}

export interface TaxExemptionOption {
  name: string;
  custom_efris_tax_category?: string | null;
}

export interface TaxExemptionOptionsResponse {
  tax_categories: TaxExemptionOption[];
  item_tax_templates: TaxExemptionOption[];
}

export function isReasonRequiredError(errorOrMessage: unknown): boolean {
  const raw =
    typeof errorOrMessage === 'string'
      ? errorOrMessage
      : parseFrappeError(errorOrMessage, '') ||
        (errorOrMessage instanceof Error ? errorOrMessage.message : String(errorOrMessage ?? ''));
  return raw.includes('POS-REASON-REQUIRED') || raw.includes('POS-REASON-NOTE-REQUIRED');
}

export async function overrideLinePrice(params: {
  invoice: string;
  itemRowName: string;
  newRate: number;
  reason?: string;
  reasonNote?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('price override');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.override_line_price',
    {
      invoice: params.invoice,
      item_row_name: params.itemRowName,
      new_rate: params.newRate,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function addOpenItem(params: {
  invoice: string;
  itemCode: string;
  qty: number;
  rate: number;
  reference?: string;
  reason?: string;
  reasonNote?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('open item');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.add_open_item',
    {
      invoice: params.invoice,
      item_code: params.itemCode,
      qty: params.qty,
      rate: params.rate,
      reference: params.reference || undefined,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function applyComplimentary(params: {
  invoice: string;
  compType: 'Complimentary' | 'House' | 'Non-Revenue';
  itemRowName?: string;
  reason?: string;
  reasonNote?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('complimentary');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.apply_complimentary',
    {
      invoice: params.invoice,
      comp_type: params.compType,
      item_row_name: params.itemRowName || undefined,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function listTaxExemptionOptions(company: string): Promise<TaxExemptionOptionsResponse> {
  const res = await call.get<{ message?: TaxExemptionOptionsResponse }>(
    'ury.ury_pos.pricing_actions.list_tax_exemption_options',
    { company },
  );
  return res?.message ?? { tax_categories: [], item_tax_templates: [] };
}

export async function applyTaxExemption(params: {
  invoice: string;
  reason?: string;
  reasonNote?: string;
  certificate?: string;
  taxCategory?: string;
  itemRowName?: string;
  itemTaxTemplate?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('tax exemption');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.apply_tax_exemption',
    {
      invoice: params.invoice,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      certificate: params.certificate || undefined,
      tax_category: params.taxCategory || undefined,
      item_row_name: params.itemRowName || undefined,
      item_tax_template: params.itemTaxTemplate || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function clearTaxExemption(params: {
  invoice: string;
  reason?: string;
  reasonNote?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('tax exemption');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.clear_tax_exemption',
    {
      invoice: params.invoice,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function adjustServiceCharge(params: {
  invoice: string;
  mode: 'waive' | 'restore' | 'override';
  rate?: number;
  amount?: number;
  reason?: string;
  reasonNote?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('service charge');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.adjust_service_charge',
    {
      invoice: params.invoice,
      mode: params.mode,
      rate: params.rate,
      amount: params.amount,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function adjustClosedCheckTip(params: {
  invoice: string;
  newTipAmount: number;
  tipServer?: string;
  modeOfPayment?: string;
  reason?: string;
  reasonNote?: string;
  approvalToken?: string;
}) {
  await assertOfflineActionAllowed('tip adjustment');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.pricing_actions.adjust_closed_check_tip',
    {
      invoice: params.invoice,
      new_tip_amount: params.newTipAmount,
      tip_server: params.tipServer || undefined,
      mode_of_payment: params.modeOfPayment || undefined,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
      approval_token: params.approvalToken || undefined,
    },
  );
  return res.message;
}

export async function transferCheckOutlet(params: {
  invoice: string;
  targetPosProfile: string;
  targetTable?: string;
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}) {
  await assertOfflineActionAllowed('outlet transfer');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.floor_actions.transfer_check_outlet',
    {
      invoice: params.invoice,
      target_pos_profile: params.targetPosProfile,
      target_table: params.targetTable || undefined,
      approval_token: params.approvalToken || undefined,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
    },
  );
  return res.message;
}

export async function pickupOtherCheck(params: {
  invoice: string;
  approvalToken?: string;
  reason?: string;
  reasonNote?: string;
}) {
  await assertOfflineActionAllowed('pick up check');
  const res = await call.post<{ message: Record<string, unknown> }>(
    'ury.ury_pos.floor_actions.pickup_other_check',
    {
      invoice: params.invoice,
      approval_token: params.approvalToken || undefined,
      reason: params.reason || undefined,
      reason_note: params.reasonNote || undefined,
    },
  );
  return res.message;
}
