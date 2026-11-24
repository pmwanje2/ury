import { call } from '@ury/core';
import { listReasons, type OverrideReason } from './override-api';
import type { CashMovementType } from './till-cash-api';

const MIN_POS_REASON_NOTE_LEN = 4;

/**
 * Align a free-text operator reason with active server-configured `POS Reason` rows
 * so #147 R2's server-side `_validate_reason` passes without hardcoding reason strings.
 */
export function resolvePosReasonForClaim(
  rawReason: string,
  activeReasons: OverrideReason[],
): { claimReason: string; needsPicker: boolean } {
  const trimmed = rawReason.trim();
  if (!trimmed) {
    return { claimReason: '', needsPicker: false };
  }
  if (!activeReasons.length) {
    return { claimReason: trimmed, needsPicker: false };
  }
  const lowered = trimmed.toLowerCase();
  for (const row of activeReasons) {
    const code = (row.code || '').trim();
    const desc = (row.description || '').trim();
    const needsNote = Number(row.requires_note) === 1;
    if ((code && lowered === code.toLowerCase()) || (desc && lowered === desc.toLowerCase())) {
      if (!needsNote) {
        return { claimReason: trimmed, needsPicker: false };
      }
      return { claimReason: '', needsPicker: true };
    }
    for (const prefix of [code, desc]) {
      if (prefix && lowered.startsWith(`${prefix.toLowerCase()}:`)) {
        const note = trimmed.slice(prefix.length + 1).trim();
        if (!needsNote || note.length >= MIN_POS_REASON_NOTE_LEN) {
          return { claimReason: trimmed, needsPicker: false };
        }
        return { claimReason: '', needsPicker: true };
      }
    }
  }
  if (trimmed.length >= MIN_POS_REASON_NOTE_LEN) {
    const catchAll = activeReasons.find(
      (row) => (!row.applies_to || row.applies_to.length === 0) && Number(row.requires_note) === 1,
    );
    if (catchAll) {
      const label = (catchAll.description || catchAll.code || '').trim();
      if (label) {
        return { claimReason: `${label}: ${trimmed}`, needsPicker: false };
      }
    }
  }
  return { claimReason: '', needsPicker: true };
}

export async function loadActivePosReasonForClaim(
  privilege: string,
  rawReason: string,
): Promise<{ claimReason: string; needsPicker: boolean }> {
  const trimmed = rawReason.trim();
  if (!trimmed) {
    return { claimReason: '', needsPicker: false };
  }
  const activeReasons = await listReasons(privilege).catch(() => []);
  return resolvePosReasonForClaim(trimmed, activeReasons);
}

export interface TillDrawerStatus {
  pos_profile: string;
  currency: string;
  opening_entry: string | null;
  opening_user: string | null;
  blind_close: boolean;
  cash_pull_threshold: number;
  threshold_exceeded: boolean;
  expected_drawer_cash: number | null;
  suggested_pickup_amount: number | null;
  threshold_exception_count: number;
  no_sale_count: number;
  over_short_tolerance: number;
  paid_out_threshold: number;
  paid_in_threshold: number;
}

export interface OpenTillRow {
  name: string;
  user: string;
  user_name?: string;
  period_start_date: string;
  pos_profile: string;
  is_own?: boolean;
}

export interface ClosedTillRow {
  name: string;
  pos_opening_entry: string;
  user: string;
  user_name?: string;
  posting_date: string;
  period_end_date?: string;
  status: string;
  force_closed?: boolean;
  no_sale_count?: number;
}

export interface TillClosePreview {
  pos_profile: string;
  pos_opening_entry: string;
  opening_user: string;
  blind_close: boolean;
  over_short_tolerance: number;
  no_sale_count: number;
  open_checks_count: number;
  open_checks: Array<{ name: string; restaurant_table?: string; grand_total?: number }>;
  pending_movements_count: number;
  pending_movements: Array<{ name: string; movement_type?: string; amount?: number }>;
}

export interface ShiftXReport {
  pos_profile: string;
  pos_opening_entry: string;
  currency: string;
  blind_close: boolean;
  expected_drawer_cash: number | null;
  net_drawer_movement: number;
  no_sale_count: number;
  no_sale_events: Array<{
    name: string;
    operator?: string;
    approver?: string;
    reason?: string;
    event_time?: string;
  }>;
  threshold_exception_count: number;
  threshold_exceptions: Array<{
    name: string;
    operator?: string;
    approver?: string;
    reason?: string;
    event_time?: string;
  }>;
  movements: Array<{
    name: string;
    movement_type: string;
    amount: number;
    reason?: string;
    reference?: string;
    status?: string;
  }>;
  open_checks_count: number;
  open_checks: Array<{ name: string; restaurant_table?: string; grand_total?: number }>;
}

export async function getTillDrawerStatus(posProfile: string): Promise<TillDrawerStatus | null> {
  try {
    const res = await call.get<{ message?: Partial<TillDrawerStatus> }>(
      'ury.ury_pos.till_controls.get_till_drawer_status',
      { pos_profile: posProfile },
    );
    const msg = res?.message;
    if (!msg || typeof msg !== 'object' || typeof msg.threshold_exceeded !== 'boolean') {
      return null;
    }
    return msg as TillDrawerStatus;
  } catch {
    return null;
  }
}

export async function recordCashMovementWithControl(payload: {
  pos_profile: string;
  movement_type: CashMovementType;
  amount: number;
  reason?: string;
  reference?: string;
  paid_out_target?: 'expense' | 'tips';
  pos_opening_entry?: string;
  approval_token?: string;
}): Promise<{ name: string; docstatus: number; status: string; approver?: string | null }> {
  const body: Record<string, unknown> = {
    pos_profile: payload.pos_profile,
    movement_type: payload.movement_type,
    amount: payload.amount,
  };
  if (payload.reason !== undefined) body.reason = payload.reason;
  if (payload.reference !== undefined) body.reference = payload.reference;
  if (payload.paid_out_target !== undefined) body.paid_out_target = payload.paid_out_target;
  if (payload.pos_opening_entry) body.pos_opening_entry = payload.pos_opening_entry;
  if (payload.approval_token) body.approval_token = payload.approval_token;
  const res = await call.post<{
    message: { name: string; docstatus: number; status: string; approver?: string | null };
  }>('ury.ury_pos.till_controls.record_cash_movement', body);
  return res.message;
}

export async function logCashPullException(payload: {
  pos_profile: string;
  reason: string;
  pos_opening_entry?: string;
  approval_token?: string;
}): Promise<{ name: string; event_type: string; approver?: string | null }> {
  const body: Record<string, unknown> = {
    pos_profile: payload.pos_profile,
    reason: payload.reason,
  };
  if (payload.pos_opening_entry) body.pos_opening_entry = payload.pos_opening_entry;
  if (payload.approval_token) body.approval_token = payload.approval_token;
  const res = await call.post<{
    message: { name: string; event_type: string; approver?: string | null };
  }>('ury.ury_pos.till_controls.log_cash_pull_exception', body);
  return res.message;
}

export async function triggerNoSale(payload: {
  pos_profile: string;
  reason: string;
  note?: string;
  pos_opening_entry?: string;
  approval_token?: string;
}): Promise<{
  event_name: string;
  drawer_kick_hex: string;
  no_sale_count_for_shift: number;
  approver?: string | null;
}> {
  const body: Record<string, unknown> = {
    pos_profile: payload.pos_profile,
    reason: payload.reason,
  };
  if (payload.note) body.note = payload.note;
  if (payload.pos_opening_entry) body.pos_opening_entry = payload.pos_opening_entry;
  if (payload.approval_token) body.approval_token = payload.approval_token;
  const res = await call.post<{
    message: {
      event_name: string;
      drawer_kick_hex: string;
      no_sale_count_for_shift: number;
      approver?: string | null;
    };
  }>('ury.ury_pos.till_controls.trigger_no_sale', body);
  return res.message;
}

export async function getShiftXReport(
  posProfile: string,
  posOpeningEntry?: string | null,
): Promise<ShiftXReport | null> {
  try {
    const query: Record<string, unknown> = { pos_profile: posProfile };
    if (posOpeningEntry) query.pos_opening_entry = posOpeningEntry;
    const res = await call.get<{ message?: Partial<ShiftXReport> }>(
      'ury.ury_pos.till_controls.get_shift_x_report',
      query,
    );
    const msg = res?.message;
    if (!msg || typeof msg !== 'object' || typeof msg.no_sale_count !== 'number') {
      return null;
    }
    return msg as ShiftXReport;
  } catch {
    return null;
  }
}

export async function listOpenTills(posProfile: string): Promise<OpenTillRow[] | null> {
  try {
    const res = await call.get<{ message?: OpenTillRow[] }>(
      'ury.ury_pos.till_controls.list_open_tills',
      { pos_profile: posProfile },
    );
    return Array.isArray(res?.message) ? res.message : null;
  } catch {
    return null;
  }
}

export async function listClosedTills(posProfile: string): Promise<ClosedTillRow[]> {
  try {
    const res = await call.get<{ message?: ClosedTillRow[] }>(
      'ury.ury_pos.till_controls.list_closed_tills',
      { pos_profile: posProfile },
    );
    return Array.isArray(res?.message) ? res.message : [];
  } catch {
    return [];
  }
}

export async function getTillClosePreview(
  posProfile: string,
  posOpeningEntry?: string,
): Promise<TillClosePreview | null> {
  try {
    const query: Record<string, unknown> = { pos_profile: posProfile };
    if (posOpeningEntry) query.pos_opening_entry = posOpeningEntry;
    const res = await call.get<{ message?: Partial<TillClosePreview> }>(
      'ury.ury_pos.till_controls.get_till_close_preview',
      query,
    );
    const msg = res?.message;
    if (!msg || typeof msg !== 'object' || typeof msg.pos_opening_entry !== 'string') {
      return null;
    }
    return msg as TillClosePreview;
  } catch {
    return null;
  }
}

export async function closeTillWithControls(payload: {
  pos_profile: string;
  pos_opening_entry: string;
  counted_payments: Array<{ mode_of_payment: string; closing_amount: number }>;
  over_short_reason?: string;
  over_short_approval_token?: string;
  close_other_approval_token?: string;
  close_other_reason?: string;
  period_end_date?: string;
}): Promise<{ name: string; docstatus: number }> {
  const body: Record<string, unknown> = {
    pos_profile: payload.pos_profile,
    pos_opening_entry: payload.pos_opening_entry,
    counted_payments: payload.counted_payments,
  };
  if (payload.over_short_reason) body.over_short_reason = payload.over_short_reason;
  if (payload.over_short_approval_token) body.over_short_approval_token = payload.over_short_approval_token;
  if (payload.close_other_approval_token) body.close_other_approval_token = payload.close_other_approval_token;
  if (payload.close_other_reason) body.close_other_reason = payload.close_other_reason;
  if (payload.period_end_date) body.period_end_date = payload.period_end_date;
  const res = await call.post<{ message: { name: string; docstatus: number } }>(
    'ury.ury_pos.till_controls.close_till_with_controls',
    body,
  );
  return res.message;
}

export async function reopenTill(payload: {
  pos_closing_entry: string;
  reason: string;
  approval_token?: string;
}): Promise<{ pos_closing_entry: string; status: string; approver?: string | null }> {
  const body: Record<string, unknown> = {
    pos_closing_entry: payload.pos_closing_entry,
    reason: payload.reason,
  };
  if (payload.approval_token) body.approval_token = payload.approval_token;
  const res = await call.post<{
    message: { pos_closing_entry: string; status: string; approver?: string | null };
  }>('ury.ury_pos.till_controls.reopen_till', body);
  return res.message;
}

export async function forceCloseTill(payload: {
  pos_profile: string;
  pos_opening_entry: string;
  counted_payments: Array<{ mode_of_payment: string; closing_amount: number }>;
  reason: string;
  discard_pending_movements?: boolean;
  approval_token?: string;
  over_short_reason?: string;
  over_short_approval_token?: string;
  period_end_date?: string;
}): Promise<{ name: string; docstatus: number; force_closed?: boolean }> {
  const body: Record<string, unknown> = {
    pos_profile: payload.pos_profile,
    pos_opening_entry: payload.pos_opening_entry,
    counted_payments: payload.counted_payments,
    reason: payload.reason,
    discard_pending_movements: Boolean(payload.discard_pending_movements),
  };
  if (payload.approval_token) body.approval_token = payload.approval_token;
  if (payload.over_short_reason) body.over_short_reason = payload.over_short_reason;
  if (payload.over_short_approval_token) body.over_short_approval_token = payload.over_short_approval_token;
  if (payload.period_end_date) body.period_end_date = payload.period_end_date;
  const res = await call.post<{
    message: { name: string; docstatus: number; force_closed?: boolean };
  }>('ury.ury_pos.till_controls.force_close_till', body);
  return res.message;
}
