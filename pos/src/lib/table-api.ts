import { assertOfflineActionAllowed } from '../offline/cashier';
import { DOCTYPES } from '../data/doctypes';
import { db, call } from '@ury/core';

export function extractErrorMessage(error: any, fallback = 'Failed to process reservation.'): string {
  if (!error) return fallback;

  if (typeof error === 'object' && error !== null) {
    if ('_server_messages' in error && typeof (error as any)._server_messages === 'string') {
      try {
        const messages = JSON.parse((error as any)._server_messages);
        if (Array.isArray(messages) && messages.length > 0) {
          const messageObj = typeof messages[0] === 'string' ? JSON.parse(messages[0]) : messages[0];
          if (messageObj && messageObj.message) {
            return messageObj.message.replace(/<[^>]*>?/gm, '').trim();
          }
        }
      } catch {}
    }

    if ('message' in error && typeof (error as any).message === 'string') {
      const msg = (error as any).message;
      if (msg.includes('_server_messages')) {
        try {
          const parsed = JSON.parse(msg);
          return extractErrorMessage(parsed, fallback);
        } catch {}
      }
      return msg.replace(/<[^>]*>?/gm, '').trim();
    }
  }

  if (error instanceof Error && error.message) {
    return error.message.replace(/<[^>]*>?/gm, '').trim();
  }

  if (typeof error === 'string') {
    return error.replace(/<[^>]*>?/gm, '').trim();
  }

  return fallback;
}

export interface Room {
  name: string;
  branch: string;
}

export interface BranchReservationSettings {
  block_dirty_table_seating?: number;
  enable_reservation: number;
  buffer_time: number;
  grace_period: number;
  /** The Branch's Minimum Reservation Notice in minutes; a negative one accepts a time that far back. */
  min_reservation_notice: number;
  avg_table_time_last_day: number;
  avg_table_time_last_week: number;
  calculated_duration: number;
}

export interface TableReservation {
  name: string;
  branch: string;
  reserved_table: string;
  customer: string;
  customer_name?: string;
  customer_phone?: string;
  no_of_pax?: number;
  reserved_at: string;
  comments?: string;
  status: 'Requested' | 'Confirmed' | 'Seated' | 'Completed' | 'Cancelled' | 'No Show';
  cancel_reason?: string;
  pos_invoice?: string;
  is_lock_window_active?: boolean;
  buffer_minutes?: number;
  grace_minutes?: number;
  duration_minutes?: number;
  lock_start_time?: string;
  /** The server's native cancel_reservation check on this booking for this user. */
  can_cancel?: boolean;
  /** The server's native mark_no_show check on this booking for this user. */
  can_mark_no_show?: boolean;
}

export type FloorTableState = 'Available' | 'Seated' | 'Check Printed' | 'Dirty';

export interface Table {
  branch?: string;
  company?: string;
  custom_table_state?: FloorTableState;
  custom_dirty_since?: string | null;
  name: string;
  occupied: number;
  latest_invoice_time: string | null;
  is_take_away: number;
  restaurant_room: string;
  table_shape: 'Circle' | 'Square' | 'Rectangle';
  merged_with?: string | null;
  no_of_seats?: number;
  layout_x?: number;
  layout_y?: number;
  minimum_seating?: number;
}


export async function getRestaurantMenu(posProfile: string, room?: string | null) {
  const params: Record<string, string> = { pos_profile: posProfile };
  if (room) {
    params.room = room;
  }
  const res = await call.get('ury.ury_pos.api.getRestaurantMenu', params);
  return res.message;
}

export async function getRooms(branch: string): Promise<Room[]> {
  const rooms = await db.getDocList(DOCTYPES.URY_ROOM, {
    fields: ['name', 'branch'],
    filters: [['branch', 'like', branch]],
    limit: 0,
    asDict: true,
  });
  return rooms as Room[];
}

export async function getTableCount(room: string, branch?: string): Promise<number> {
  const filters = [
    ['restaurant_room', '=', room],
    ...(branch ? [['branch', '=', branch]] : []),
  ];
  const countValue = await db.getCount(DOCTYPES.URY_TABLE, filters as any);
  return typeof countValue === 'number' ? countValue : Number(countValue) || 0;
}
export async function getTables(room: string): Promise<Table[]> {
  const tables = await db.getDocList(DOCTYPES.URY_TABLE, {
    fields: [
      'name',
      'occupied',
      'custom_table_state',
      'custom_dirty_since',
      'latest_invoice_time',
      'is_take_away',
      'restaurant_room',
      'table_shape',
      'merged_with',
      'no_of_seats',
      'layout_x',
      'layout_y',
      'minimum_seating'
    ],
    filters: [['restaurant_room', '=', room]],
    limit: 0,
    asDict: true,
  });

  return tables as Table[];
}

// Historical export name used by the table and captain screens.
export async function getVacantTablesForBranch(branch: string, excludeTable?: string): Promise<Table[]> {
  const result = await call.get<{ message: Table[] }>('ury.ury_pos.floor_actions.get_transfer_destinations', {
    branch, exclude_table: excludeTable,
  });
  return result.message;
}

export async function updateTableLayout(name: string, data: Partial<Table>) {
  return db.updateDoc(DOCTYPES.URY_TABLE, name, data);
}

export async function mergeTablesBatch(
  anchor: string,
  tables: string[],
  auth?: { approvalToken?: string; reason?: string; reasonNote?: string },
) {
  await assertOfflineActionAllowed('merge tables');
  return call.post('ury.ury.doctype.ury_order.ury_order.merge_tables_batch', {
    anchor_table: anchor,
    tables,
    ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
    ...(auth?.reason ? { reason: auth.reason } : {}),
    ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
  });
}

export async function unmergeTables(table: string) {
  return call.post('ury.ury.doctype.ury_order.ury_order.unmerge_tables', {
    table,
  });
}


// The Branch owns every reservation timing; without the server's settings the POS
// reads reservations as off and keeps no timings of its own.
export async function getBranchReservationSettings(branch: string): Promise<BranchReservationSettings | null> {
  const { call } = await import('@ury/core');

  const response = await call.get(
    'ury.ury.api.table_reservation.get_branch_reservation_settings',
    { branch }
  );

  return response.message ?? null;
}

export async function checkTableReservation(table: string): Promise<TableReservation | null> {
  const { call } = await import('@ury/core');

  const response = await call.get(
    'ury.ury.api.table_reservation.check_table_reservation',
    { table }
  );

  return response.message ?? null;
}

export async function createTableReservation(data: {
  table: string;
  customer: string;
  customer_name?: string;
  customer_phone: string;
  no_of_pax: number;
  reserved_at: string;
  notes?: string;
  branch?: string;
}) {
  const { call } = await import('@ury/core');

  const response = await call.post(
    'ury.ury.api.table_reservation.create_table_reservation',
    {
      table: data.table,
      customer: data.customer,
      customer_name: data.customer_name,
      customer_phone: data.customer_phone,
      no_of_pax: data.no_of_pax,
      reserved_at: data.reserved_at,
      notes: data.notes,
      branch: data.branch,
    }
  );

  return response.message;
}

export async function updateTableReservation(data: {
  reservation_name: string;
  table?: string;
  customer?: string;
  customer_name?: string;
  customer_phone?: string;
  no_of_pax?: number;
  reserved_at?: string;
  notes?: string;
  branch?: string;
}) {
  const { call } = await import('@ury/core');

  const response = await call.post(
    'ury.ury.api.table_reservation.update_table_reservation',
    data
  );

  return response.message;
}

export async function updateTableReservationStatus(
  reservationName: string,
  status: string,
  posInvoice?: string,
  cancelReason?: string,
  auth?: { approvalToken?: string; reasonNote?: string; posProfile?: string },
) {
  if (status === 'Cancelled' || status === 'No Show') {
    await assertOfflineActionAllowed('reservation status');
  }
  const { call } = await import('@ury/core');

  const response = await call.post(
    'ury.ury.api.table_reservation.update_reservation_status',
    {
      reservation_name: reservationName,
      status,
      pos_invoice: posInvoice,
      cancel_reason: cancelReason,
      ...(auth?.approvalToken ? { approval_token: auth.approvalToken } : {}),
      ...(auth?.reasonNote ? { reason_note: auth.reasonNote } : {}),
      ...(auth?.posProfile ? { pos_profile: auth.posProfile } : {}),
    }
  );

  return response.message;
}

export async function getActiveReservations(branch?: string, date?: string): Promise<TableReservation[]> {
  const { call } = await import('@ury/core');

  const response = await call.get('ury.ury.api.table_reservation.get_active_reservations', {
    branch: branch || undefined,
    date: date || undefined,
  });
  return response.message ?? [];
}

export async function getAvailableTablesForReservation(
  branch: string,
  reservedAt: string,
  excludeReservation?: string,
  noOfPax?: number
): Promise<Table[]> {
  const { call } = await import('@ury/core');

  const response = await call.get(
    'ury.ury.api.table_reservation.get_available_tables_for_reservation',
    {
      branch,
      reserved_at: reservedAt,
      exclude_reservation: excludeReservation || undefined,
      // Only tables the party fits (seats and minimum seating) are offered.
      no_of_pax: noOfPax || undefined,
    }
  );
  return response.message ?? [];
}


export async function markTableClean(table: string): Promise<void> {
  await call.post('ury.ury.api.table_lifecycle.mark_table_clean', { table });
}

export interface WaitlistParty {
  name: string;
  customer_name: string;
  customer_phone: string;
  no_of_pax: number;
  requested_room?: string;
  quoted_wait_minutes: number;
  elapsed_minutes: number;
  status: 'Waiting' | 'Waitlist Notified';
}

const WAITLIST_API = 'ury.ury.api.table_reservation';

export async function listWaitlist(branch: string): Promise<WaitlistParty[]> {
  return (await call.get(`${WAITLIST_API}.list_walkin_waitlist`, { branch })).message;
}

export async function estimateWait(branch: string, no_of_pax: number, requested_room?: string): Promise<number | null> {
  const response = await call.get(`${WAITLIST_API}.estimate_wait_time`, { branch, no_of_pax, requested_room });
  return response.message.quoted_wait_minutes;
}

export async function addWalkIn(data: {
  branch: string; customer_name: string; customer_phone: string; no_of_pax: number;
  requested_room?: string; quoted_wait_minutes?: number;
}): Promise<void> {
  await call.post(`${WAITLIST_API}.add_walkin_waitlist`, data);
}

export async function notifyWaitlistParty(reservation: string): Promise<void> {
  await call.post(`${WAITLIST_API}.notify_waitlist_party`, { reservation });
}

export async function seatWaitlistParty(reservation: string, table: string): Promise<void> {
  await call.post(`${WAITLIST_API}.seat_waitlist_party`, { reservation, table });
}
