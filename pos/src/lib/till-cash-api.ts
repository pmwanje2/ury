import { call } from '@ury/core';

export type CashMovementType = 'Pickup' | 'Loan' | 'Paid Out' | 'Paid In';

export interface TillCashPaidOutTarget {
  kind: 'expense' | 'tips';
  label: string;
}

export interface TillCashServerOption {
  user: string;
  full_name: string;
}

export interface TillCashMovementRow {
  name: string;
  movement_type: CashMovementType;
  amount: number;
  drawer_change: number;
  reason: string;
  reference?: string;
  target_kind: 'safe' | 'expense' | 'tips' | 'paid_in';
  tip_server?: string;
  tip_server_name?: string;
  status: 'Submitted' | 'Pending Approval';
  docstatus: number;
  owner: string;
  owner_name: string;
  approver: string;
  approver_name: string;
  creation: string;
  can_approve: boolean;
  can_discard: boolean;
}

export interface TillCashState {
  enabled: boolean;
  pos_profile: string;
  company: string;
  currency: string;
  opening_entry: string | null;
  opening_user: string | null;
  paid_out_threshold: number | null;
  paid_in_threshold?: number;
  cash_pull_threshold?: number;
  can_approve: boolean;
  current_user: string;
  available_types: Partial<Record<CashMovementType, boolean>>;
  paid_out_targets: TillCashPaidOutTarget[];
  servers?: TillCashServerOption[];
  movements: TillCashMovementRow[];
  net_drawer_movement: number;
  pending_count: number;
}

export interface ShiftCashMovementTotals {
  by_mode: Record<string, number>;
  pending_count: number;
}

export async function getTillCashState(posProfile: string): Promise<TillCashState> {
  const response = await call.get<{ message: TillCashState }>(
    'ury.ury_pos.cash_movements.get_till_cash_state',
    { pos_profile: posProfile },
  );
  return response.message;
}

export async function recordTillCashMovement(payload: {
  pos_profile: string;
  movement_type: CashMovementType;
  amount: number;
  reason?: string;
  reference?: string;
  paid_out_target?: 'expense' | 'tips';
  tip_server?: string;
  approval_token?: string;
}): Promise<TillCashMovementRow> {
  const body: Record<string, unknown> = {
    pos_profile: payload.pos_profile,
    movement_type: payload.movement_type,
    amount: payload.amount,
  };
  if (payload.reason !== undefined) {
    body.reason = payload.reason;
  }
  if (payload.reference) {
    body.reference = payload.reference;
  }
  if (payload.paid_out_target !== undefined) {
    body.paid_out_target = payload.paid_out_target;
  }
  if (payload.tip_server) {
    body.tip_server = payload.tip_server;
  }
  if (payload.approval_token) {
    body.approval_token = payload.approval_token;
  }
  const response = await call.post<{ message: TillCashMovementRow }>(
    'ury.ury_pos.cash_movements.record_till_cash_movement',
    body,
  );
  return response.message;
}

export async function approveTillCashMovement(
  journalEntry: string,
  approvalToken?: string,
  approvalReason?: string,
): Promise<TillCashMovementRow> {
  const body: Record<string, unknown> = { journal_entry: journalEntry };
  if (approvalToken) {
    body.approval_token = approvalToken;
  }
  if (approvalReason) {
    body.approval_reason = approvalReason;
  }
  const response = await call.post<{ message: TillCashMovementRow }>(
    'ury.ury_pos.cash_movements.approve_till_cash_movement',
    body,
  );
  return response.message;
}

export async function discardTillCashMovement(journalEntry: string): Promise<{ name: string; status: string }> {
  const response = await call.post<{ message: { name: string; status: string } }>(
    'ury.ury_pos.cash_movements.discard_till_cash_movement',
    { journal_entry: journalEntry },
  );
  return response.message;
}

export async function getShiftCashMovementTotals(posOpeningEntry: string): Promise<ShiftCashMovementTotals> {
  try {
    const response = await call.get<{ message?: Partial<ShiftCashMovementTotals> }>(
      'ury.ury_pos.cash_movements.get_shift_cash_movement_totals',
      { pos_opening_entry: posOpeningEntry },
    );
    const msg = response?.message;
    if (!msg || typeof msg !== 'object') {
      return { by_mode: {}, pending_count: 0 };
    }
    return {
      by_mode: msg.by_mode && typeof msg.by_mode === 'object' ? msg.by_mode : {},
      pending_count: Number(msg.pending_count) || 0,
    };
  } catch {
    return { by_mode: {}, pending_count: 0 };
  }
}
