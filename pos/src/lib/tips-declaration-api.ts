import { call } from '@ury/core';

export interface ShiftJobSummaryRow {
  job_code: string | null;
  labor_minutes: number;
  charged_tips: number;
}

export interface ShiftTipDeclarationRow {
  user: string;
  employee: string | null;
  full_name: string;
  currency: string;
  charged_tips_collected: number;
  tips_paid_from_till: number;
  net_tips_payable: number;
  declared_cash_tips: number;
  remarks: string;
}

export interface ShiftTipDeclarationResponse {
  pos_opening_entry: string;
  pos_profile: string;
  company?: string;
  currency: string;
  currency_precision: number;
  require_tip_declaration: boolean;
  rows: ShiftTipDeclarationRow[];
  job_summary?: ShiftJobSummaryRow[];
}

export async function getShiftTipDeclarationRows(
  posOpeningEntry: string,
): Promise<ShiftTipDeclarationResponse | null> {
  try {
    const res = await call.get<{ message: ShiftTipDeclarationResponse }>(
      'ury.ury_pos.tips_declaration.get_shift_tip_declaration_rows',
      { pos_opening_entry: posOpeningEntry },
    );
    const data = res?.message;
    if (!data || !Array.isArray(data.rows)) {
      return null;
    }
    return data;
  } catch {
    return null;
  }
}
