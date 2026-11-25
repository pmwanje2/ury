import { call } from '@ury/core';
import { messageToPlainText } from '@ury/ui';

export interface TimeclockStatus {
  available: boolean;
  has_employee?: boolean;
  employee?: string | null;
  employee_name?: string | null;
  is_clocked_in?: boolean;
  last_log_type?: 'IN' | 'OUT' | null;
  last_punch_time?: string | null;
  clocked_in_since?: string | null;
  elapsed_minutes?: number;
  shift?: string | null;
  open_checks_count?: number;
  open_checks?: string[];
  has_open_shift?: boolean;
  open_shift?: string | null;
  block_clock_out_with_open_work?: boolean;
  require_clock_in_to_order?: boolean;
  pos_profile?: string | null;
}

export interface TimeclockPunchResponse extends TimeclockStatus {
  checkin?: string;
  log_type?: 'IN' | 'OUT';
  time?: string;
  device_id?: string;
}

function extractTimeclockError(error: unknown, fallback: string): Error {
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

export async function getMyTimeclockStatus(posProfile?: string | null): Promise<TimeclockStatus> {
  try {
    const params = posProfile ? { pos_profile: posProfile } : undefined;
    const res = await call.get<{ message: TimeclockStatus }>(
      'ury.ury_pos.timeclock.get_my_timeclock_status',
      params,
    );
    return res?.message ?? { available: false };
  } catch {
    return { available: false };
  }
}

export async function punchTimeclock(
  action: 'IN' | 'OUT',
  posProfile: string,
): Promise<TimeclockPunchResponse> {
  try {
    const res = await call.post<{ message: TimeclockPunchResponse }>(
      'ury.ury_pos.timeclock.punch_timeclock',
      {
        action,
        pos_profile: posProfile,
      },
    );
    return res.message;
  } catch (error) {
    throw extractTimeclockError(error, 'Could not record timeclock punch.');
  }
}
