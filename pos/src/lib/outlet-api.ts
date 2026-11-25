import { call } from '@ury/core';
import { messageToPlainText } from '@ury/ui';
import type { PosProfileLimited } from './pos-profile-api';

export interface AvailableOutlet {
  pos_profile: string;
  name: string;
  branch: string;
  company: string;
  restaurant: string;
  currency: string;
  has_open_shift: boolean;
  open_entry: string | null;
  open_business_date?: string | null;
  is_active?: boolean;
}

export interface SetActiveOutletResponse extends PosProfileLimited {
  active_outlet: string;
  available_outlets: AvailableOutlet[];
  open_till_elsewhere?: {
    pos_profile: string;
    opening_entry: string;
    branch: string;
  } | null;
}

export interface ShiftReportColumn {
  fieldname: string;
  label: string;
  fieldtype?: string;
  options?: string;
}

export interface ShiftReportSummaryItem {
  label?: string;
  value?: number | string;
  datatype?: string;
  currency?: string;
  indicator?: string;
}

export interface ShiftReportResponse {
  available: boolean;
  has_open_shift?: boolean;
  report_type?: 'X' | 'Z';
  pos_profile?: string;
  company?: string;
  currency?: string;
  pos_opening_entry?: string | null;
  business_date?: string | null;
  posting_date?: string | null;
  columns?: ShiftReportColumn[];
  rows?: Array<Record<string, unknown> | unknown[]>;
  report_summary?: ShiftReportSummaryItem[];
  message?: string;
  has_z_print_format?: boolean;
}

function extractOutletError(error: unknown, fallback: string): Error {
  if (error && typeof error === 'object') {
    const raw = (error as { _server_messages?: string })._server_messages;
    if (typeof raw === 'string') {
      try {
        const list = JSON.parse(raw);
        const first = typeof list[0] === 'string' ? JSON.parse(list[0]) : list[0];
        if (first?.message) {
          return new Error(messageToPlainText(String(first.message)));
        }
      } catch {
        // fall through
      }
    }
    const msg = (error as { message?: unknown }).message;
    if (typeof msg === 'string' && msg.trim()) {
      return new Error(messageToPlainText(msg));
    }
  }
  if (error instanceof Error) return error;
  return new Error(fallback);
}

export async function getAvailableOutlets(): Promise<AvailableOutlet[]> {
  try {
    const res = await call.get<{ message: AvailableOutlet[] }>(
      'ury.ury_pos.outlet_context.get_available_outlets',
    );
    return Array.isArray(res?.message) ? res.message : [];
  } catch {
    return [];
  }
}

export async function setActiveOutlet(posProfile: string): Promise<SetActiveOutletResponse> {
  try {
    const res = await call.post<{ message: SetActiveOutletResponse }>(
      'ury.ury_pos.outlet_context.set_active_outlet',
      { pos_profile: posProfile },
    );
    return res.message;
  } catch (error) {
    throw extractOutletError(error, 'Could not switch outlet.');
  }
}

export async function getShiftXReport(
  posProfile?: string | null,
  posOpeningEntry?: string | null,
): Promise<ShiftReportResponse> {
  try {
    const params: Record<string, string> = {};
    if (posProfile) params.pos_profile = posProfile;
    if (posOpeningEntry) params.pos_opening_entry = posOpeningEntry;
    const res = await call.get<{ message: ShiftReportResponse }>(
      'ury.ury_pos.outlet_context.get_shift_x_report',
      Object.keys(params).length ? params : undefined,
    );
    return res?.message ?? { available: false };
  } catch (error) {
    throw extractOutletError(error, 'Could not load shift report.');
  }
}
