import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { call } from '@ury/core';
import SplitInvoiceDetails from '../components/SplitInvoiceDetails';
import { getPOSInvoiceItems, getSplitGroup, type POSInvoiceItem } from '../../lib/invoice-api';
import Notifications from '../../components/Notifications';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, Square } from 'lucide-react';
import { Button, Spinner, showToast } from '@ury/ui';
import { useCaptainContext } from '../hooks/useCaptainContext';
import { getRooms, getTables, checkTableReservation, type Room, type Table } from '../../lib/table-api';
import { getTableOrderContext } from '../../lib/table-order-context-api';
import { getTableOrder } from '../../lib/order-api';
import { getMergeGroupMembers, sortTablesByMergeGroups, formatReservationTime, isReservationLockWindowActive } from '../../lib/table-utils';
import { t } from '../../i18n';
import { usePOSStore } from '../../store/pos-store';
import {
  getActiveTableOrders,
  getUpcomingBookings,
  getUserFullNames,
  type ActiveTableOrder,
  type UpcomingBooking,
} from '../lib/captain-table-api';
import { useOpenServiceRequestTables } from '../../components/ServiceRequestAlert';
import { floorSignOut } from '../../lib/floor-session';
import { useRootStore } from '../../store/root-store';
import { useOutbox } from '../../offline/use-outbox';
import { createOfflinePeerSync, type OfflinePeerTableSummary } from '../../offline/peer-sync';
import CaptainTableCard, {
  type CaptainTableOwnership,
} from '../components/CaptainTableCard';

const CAPTAIN_ROOMS_CACHE_PREFIX = 'ury_captain_rooms_cache:';
const CAPTAIN_TABLES_CACHE_PREFIX = 'ury_captain_tables_cache:';

function readCachedRooms(branchName: string): Room[] {
  if (typeof localStorage === 'undefined' || !branchName) return [];
  try {
    const raw = localStorage.getItem(`${CAPTAIN_ROOMS_CACHE_PREFIX}${branchName}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Room[]) : [];
  } catch {
    return [];
  }
}

function saveCachedRooms(branchName: string, roomsList: Room[]): void {
  if (typeof localStorage === 'undefined' || !branchName) return;
  try {
    localStorage.setItem(`${CAPTAIN_ROOMS_CACHE_PREFIX}${branchName}`, JSON.stringify(roomsList));
  } catch {
    // Ignore storage quota errors.
  }
}

function readCachedTables(roomName: string): Table[] | null {
  if (typeof localStorage === 'undefined' || !roomName) return null;
  try {
    const raw = localStorage.getItem(`${CAPTAIN_TABLES_CACHE_PREFIX}${roomName}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Table[]) : null;
  } catch {
    return null;
  }
}

function saveCachedTables(roomName: string, tableList: Table[]): void {
  if (typeof localStorage === 'undefined' || !roomName) return;
  try {
    localStorage.setItem(`${CAPTAIN_TABLES_CACHE_PREFIX}${roomName}`, JSON.stringify(tableList));
  } catch {
    // Ignore storage quota errors.
  }
}

/**
 * Captain "Tables" home screen (`/order`). See PLAN.md §5/§6 for the
 * intended IA. Mobile-first: large single-tap table cards, room tabs,
 * no cashier-oriented chrome (no payment/discount/settlement affordances —
 * those never belong on this screen for a Captain, per PLAN §6).
 */
export default function CaptainTables() {
  const navigate = useNavigate();
  const serviceRequestTables = useOpenServiceRequestTables();
  const {
    context,
    capabilities,
    branch,
    rooms,
    isLoading: contextLoading,
    error: contextError,
  } = useCaptainContext();

  const posProfile = usePOSStore((s) => s.posProfile);
  const tillCurrency = posProfile?.currency || usePOSStore((s) => s.currency);
  const rootUser = useRootStore((s) => s.user);
  const { rows: outboxRows, offline: outboxOffline } = useOutbox(posProfile, rootUser, true);
  const isOffline = outboxOffline || (typeof navigator !== 'undefined' && navigator.onLine === false);
  const [peerPendingTables, setPeerPendingTables] = useState<OfflinePeerTableSummary[]>([]);
  const currentUser = context?.user ?? null;
  const canAccessOtherCaptainsTables = Boolean(capabilities?.canAccessOtherCaptainsTables);

  useEffect(() => {
    if (!branch || !posProfile?.name || !posProfile.ury_offline_capture) return;
    const sync = createOfflinePeerSync({
      branch,
      posProfile: posProfile.name,
      relayUrl: posProfile.custom_offline_peer_relay_url,
      onPeerUpdate: (msg) => {
        if (msg.type === 'outbox_snapshot') {
          setPeerPendingTables(msg.pendingTables || []);
        } else if (msg.type === 'outbox_cleared') {
          setPeerPendingTables([]);
        }
      },
    });
    void sync.pollRelay();
    return () => sync.close();
  }, [branch, posProfile?.name, posProfile?.ury_offline_capture, posProfile?.custom_offline_peer_relay_url]);

  const queuedOfflineCount = useMemo(() => {
    const ids = new Set<string>();
    for (const row of outboxRows) {
      if (row.state === 'pending') ids.add(row.client_order_id);
    }
    for (const peer of peerPendingTables) {
      if (peer.clientOrderId) ids.add(peer.clientOrderId);
    }
    return ids.size;
  }, [outboxRows, peerPendingTables]);

  const [selectedRoom, setSelectedRoom] = useState<string | null>(null);
  const [branchRooms, setBranchRooms] = useState<Room[]>([]);
  const [roomsLoading, setRoomsLoading] = useState(false);
  const [tables, setTables] = useState<Table[]>([]);
  const [activeOrders, setActiveOrders] = useState<Map<string, ActiveTableOrder>>(new Map());
  const [bookings, setBookings] = useState<Map<string, UpcomingBooking>>(new Map());
  const [ownerNames, setOwnerNames] = useState<Map<string, string>>(new Map());
  const [tablesLoading, setTablesLoading] = useState(false);
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [readyCheck, setReadyCheck] = useState<{ name: string; items: POSInvoiceItem[] } | null>(null);
  const [showNotifications, setShowNotifications] = useState(false);

  // `get_captain_context()`'s `rooms` field (from `getRoom()` in
  // `ury/ury_pos/api.py`) reflects the Captain's own room *assignment*, not
  // "which rooms exist" — a Captain assigned at the branch level (not a
  // specific room) gets back a row with `name: null`, which is not a usable
  // room selector value. The Cashier `Table.tsx` doesn't use `getRoom()` for
  // its room selector at all; it lists ALL branch rooms via `getRooms(branch)`
  // and lets the user pick. Match that pattern here — every table needs to be
  // reachable regardless of whether this Captain has a narrow room
  // assignment, since room-level restriction (if any) is enforced
  // server-side (captain_transfer's room-match, table_transfer's branch
  // check), not by hiding rooms from the picker.
  const roomsRef = useRef(rooms);
  roomsRef.current = rooms;

  useEffect(() => {
    if (!branch) return;
    let cancelled = false;
    setRoomsLoading(true);
    getRooms(branch)
      .then((fetched) => {
        if (cancelled) return;
        saveCachedRooms(branch, fetched);
        setBranchRooms(fetched);
      })
      .catch(() => {
        if (cancelled) return;
        const cached = readCachedRooms(branch);
        if (cached.length > 0) {
          setBranchRooms(cached);
        } else if (roomsRef.current.some((r) => r.name)) {
          setBranchRooms(roomsRef.current.filter((r) => r.name));
        } else {
          setBranchRooms([]);
        }
      })
      .finally(() => {
        if (!cancelled) setRoomsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [branch]);

  // Default the selected tab to the Captain's own assigned room if they have
  // one (a real, non-null room name from get_captain_context), else the
  // first branch room.
  useEffect(() => {
    if (selectedRoom || branchRooms.length === 0) return;
    const assignedRoomName = rooms.find((r) => r.name)?.name;
    const defaultRoom = branchRooms.find((r) => r.name === assignedRoomName) ?? branchRooms[0];
    setSelectedRoom(defaultRoom.name);
  }, [branchRooms, rooms, selectedRoom]);

  const loadTables = useCallback(async (roomName: string) => {
    setTablesLoading(true);
    setTablesError(null);
    try {
      const fetchedTables = await getTables(roomName);
      const sorted = sortTablesByMergeGroups(fetchedTables);
      saveCachedTables(roomName, sorted);
      setTables(sorted);
    } catch (err) {
      const cached = readCachedTables(roomName);
      if (cached) {
        setTables(sortTablesByMergeGroups(cached));
        setTablesError(null);
      } else {
        console.error(err);
        setTablesError('Failed to load tables');
        setTables([]);
      }
    } finally {
      setTablesLoading(false);
    }
  }, []);

  const loadActiveOrders = useCallback(async (branchName: string) => {
    if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
    try {
      const orders = await getActiveTableOrders(branchName);
      setActiveOrders(orders);

      const waiters = Array.from(orders.values()).map((order) => order.waiter);
      const names = await getUserFullNames(waiters);
      setOwnerNames(names);
    } catch (err) {
      // Non-fatal: table grid still renders with occupied/free state from
      // the bulk table list, just without waiter/total annotations.
      const isOfflineErr =
        (typeof navigator !== 'undefined' && navigator.onLine === false) ||
        Boolean((err as { ury_network_failure?: boolean })?.ury_network_failure);
      if (!isOfflineErr) {
        console.error('Failed to load active table orders', err);
      }
    }
  }, []);

  const loadBookings = useCallback(async (branchName: string) => {
    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
      setBookings(new Map());
      return;
    }
    try {
      setBookings(await getUpcomingBookings(branchName));
    } catch (err) {
      // Advisory only: a booking read must never prevent seating.
      setBookings(new Map());
      const isOfflineErr =
        (typeof navigator !== 'undefined' && navigator.onLine === false) ||
        Boolean((err as { ury_network_failure?: boolean })?.ury_network_failure);
      if (!isOfflineErr) {
        console.error('Failed to load table bookings', err);
      }
    }
  }, []);

  useEffect(() => {
    if (selectedRoom) {
      loadTables(selectedRoom);
    }
  }, [selectedRoom, loadTables]);

  useEffect(() => {
    if (branch) {
      loadActiveOrders(branch);
      loadBookings(branch);
    }
  }, [branch, loadActiveOrders, loadBookings]);

  const refresh = useCallback(() => {
    if (selectedRoom) loadTables(selectedRoom);
    if (branch) {
      loadActiveOrders(branch);
      loadBookings(branch);
    }
  }, [selectedRoom, branch, loadTables, loadActiveOrders, loadBookings]);

  const tableGroups = useMemo(() => sortTablesByMergeGroups(tables), [tables]);

  const resolveOwnership = useCallback(
    (table: Table, order: ActiveTableOrder | undefined): CaptainTableOwnership => {
      if (table.occupied !== 1) return 'free';
      if (!order) return 'occupied-unknown';
      return order.waiter === currentUser ? 'mine' : 'other';
    },
    [currentUser]
  );

  const handleTableTap = async (table: Table, order: ActiveTableOrder | undefined) => {
    const ownership = resolveOwnership(table, order);

    if (context?.reservation_enabled !== true) {
      if (ownership === 'free' || ownership === 'mine' || canAccessOtherCaptainsTables) {
        navigate(`/order/table/${table.name}`);
      } else {
        const name = order && (ownerNames.get(order.waiter) ?? order.waiter);
        showToast.error(name ? t('tables.assigned_to', { owner: name }) : t('tables.table_occupied'));
      }
      return;
    }
    if (ownership === 'free') {
      try {
        const orderRes = await getTableOrder(table.name);
        const existingInvoice = orderRes?.message;
        if (
          existingInvoice &&
          existingInvoice.name &&
          existingInvoice.docstatus === 0 &&
          existingInvoice.invoice_printed !== 1
        ) {
          navigate(`/order/table/${table.name}`);
          return;
        }
      } catch {}

      try {
        const res = await checkTableReservation(table.name);
        if (
          res &&
          (res.is_lock_window_active || isReservationLockWindowActive(res)) &&
          res.status === 'Confirmed'
        ) {
          const timeStr = formatReservationTime(res.reserved_at);
          showToast.error(t('tables.reserved_choose_another', { table: table.name, time: timeStr }));
          return;
        }
      } catch (err) {
        console.error(err);
      }
      navigate(`/order/table/${table.name}`);
      return;
    }

    if (ownership === 'mine') {
      navigate(`/order/table/${table.name}`);
      return;
    }

    // Occupied table with NO active order in activeOrders (e.g. Completed reservation table)
    if (ownership === 'occupied-unknown') {
      try {
        const orderRes = await getTableOrder(table.name);
        const existingInvoice = orderRes?.message;
        if (
          existingInvoice &&
          existingInvoice.name &&
          existingInvoice.docstatus === 0
        ) {
          if (
            existingInvoice.waiter === currentUser ||
            canAccessOtherCaptainsTables
          ) {
            navigate(`/order/table/${table.name}`);
            return;
          }
          const waiterName = existingInvoice.waiter
            ? (ownerNames.get(existingInvoice.waiter) ?? existingInvoice.waiter)
            : null;
          showToast.error(waiterName ? t('tables.assigned_to', { owner: waiterName }) : t('tables.table_occupied'));
          return;
        }
      } catch {}

      // Table is occupied but has no active invoice/waiter yet (e.g. Completed reservation).
      // Verify no active Confirmed reservation lock window before opening table.
      try {
        const res = await checkTableReservation(table.name);
        if (
          res &&
          (res.is_lock_window_active || isReservationLockWindowActive(res)) &&
          res.status === 'Confirmed'
        ) {
          const timeStr = formatReservationTime(res.reserved_at);
          showToast.error(t('tables.reserved_choose_another', { table: table.name, time: timeStr }));
          return;
        }
      } catch (err) {
        console.error(err);
      }

      // Any permitted Order Taker can access and take the order
      navigate(`/order/table/${table.name}`);
      return;
    }

    // Occupied by someone else:
    // elevated/transfer access overrides the base restriction.
    if (canAccessOtherCaptainsTables) {
      navigate(`/order/table/${table.name}`);
      return;
    }

    const ownerName = order ? ownerNames.get(order.waiter) ?? order.waiter : null;
    showToast.error(ownerName ? t('tables.assigned_to', { owner: ownerName }) : t('tables.table_occupied'));
  };

  const openKitchenCheck = useCallback(async (kotName: string, isCurrentSession: () => boolean) => {
    try {
      const response = await call.get<{ message: { restaurant_table?: string | null; invoice?: string } }>('frappe.client.get', {
        doctype: 'URY KOT',
        name: kotName,
      });
      const tableName = response.message?.restaurant_table;
      const invoice = response.message?.invoice;
      if (!isCurrentSession() || !tableName || !invoice) return false;
      const target = await getTableOrderContext(tableName);
      if (!isCurrentSession() || !target) return false;
      if (target.order?.name === invoice) {
        if (!target.permissions?.view) return false;
        navigate(`/order/table/${encodeURIComponent(tableName)}`);
      } else {
        if (!target.order) {
          const group = await getSplitGroup(invoice);
          if (!isCurrentSession() || !group.invoices.some(check => check.name === invoice &&
            !check.is_original && check.restaurant_table === tableName && check.docstatus === 0)) return false;
        }
        const { items } = await getPOSInvoiceItems(invoice);
        if (!isCurrentSession()) return false;
        setReadyCheck({ name: invoice, items });
      }
      return true;
    } catch {
      return false;
    }
  }, [navigate]);

  const isLoading = contextLoading || (roomsLoading && branchRooms.length === 0);
  const error = contextError;

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner message={t('captain.loading_context')} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="text-center">
          <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-red-600" />
          <h2 className="mb-1 text-lg font-semibold text-gray-800">{t('captain.unable_load_tables')}</h2>
          <p className="text-sm text-gray-600">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-gray-50">
      <div className="sticky top-0 z-10 border-b border-border bg-white p-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-lg font-bold text-gray-900">{t('captain.tables')}</h1>
            {isOffline && (
              <span
                data-testid="captain-offline-pill"
                className="inline-flex items-center rounded-full bg-amber-100 border border-amber-300 px-2.5 py-0.5 text-xs font-medium text-amber-900"
              >
                {t('offline.waiter_offline_pill')}
              </span>
            )}
            {queuedOfflineCount > 0 && (
              <span
                data-testid="captain-queued-badge"
                className="inline-flex items-center rounded-full bg-amber-600 px-2.5 py-0.5 text-xs font-semibold text-white"
              >
                {t('offline.waiter_queued_badge', { count: queuedOfflineCount })}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={refresh}>
              {t('common.refresh')}
            </Button>
            <Button variant="outline" size="sm" onClick={() => void floorSignOut()}>
              {t('header.switch_waiter')}
            </Button>
          </div>
        </div>

        {branchRooms.length > 0 && (
          <div className="mt-2 flex gap-2 overflow-x-auto pb-1">
            {branchRooms.map((room) => (
              <Button
                key={room.name}
                variant="tab"
                size="sm"
                data-selected={selectedRoom === room.name}
                onClick={() => setSelectedRoom(room.name)}
                className="h-9 shrink-0"
              >
                {room.name}
              </Button>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-3 p-3 xl:flex-row xl:items-start">
      <div className="min-w-0 flex-1">
        {branchRooms.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 py-16 text-gray-500">
            <AlertTriangle className="h-8 w-8" />
            <p className="text-sm">{t('captain.no_rooms')}</p>
          </div>
        ) : tablesError ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 py-16 text-red-500">
            <AlertTriangle className="h-8 w-8" />
            <p className="text-sm">{tablesError}</p>
          </div>
        ) : tablesLoading ? (
          <Spinner message={t('common.loading_tables')} />
        ) : tableGroups.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 py-16 text-gray-500">
            <Square className="h-8 w-8" />
            <p className="text-sm">{t('captain.no_tables_in_room')}</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
            {tableGroups.map((table) => {
              const order = activeOrders.get(table.name);
              const ownership = resolveOwnership(table, order);
              const ownerName = order ? ownerNames.get(order.waiter) ?? order.waiter : undefined;
              const mergePartners = getMergeGroupMembers(table, tables).filter(
                (name) => name !== table.name
              );

              return (
                <CaptainTableCard
                  currency={tillCurrency}
                  key={table.name}
                  table={table}
                  order={order}
                  booking={bookings.get(table.name)}
                  ownership={ownership}
                  ownerName={ownership === 'mine' ? undefined : ownerName}
                  mergePartners={mergePartners}
                  onTap={() => handleTableTap(table, order)}
                  serviceRequest={serviceRequestTables.get(table.name) ?? null}
                />
              );
            })}
          </div>
        )}
      </div>
      {readyCheck && <SplitInvoiceDetails invoice={readyCheck} loadedItems={readyCheck.items} onClose={() => setReadyCheck(null)} />}
      <aside className="w-full shrink-0 xl:sticky xl:top-32 xl:w-80">
        <Button
          variant="outline"
          className="w-full xl:hidden"
          aria-expanded={showNotifications}
          aria-controls="floor-kitchen-notifications"
          onClick={() => setShowNotifications(value => !value)}
        >
          {t('notifications.kitchen')}
        </Button>
        <div id="floor-kitchen-notifications" className={showNotifications ? 'mt-2 xl:mt-0' : 'hidden xl:block'}>
          <Notifications
            title={t('notifications.kitchen')}
            documentType="URY KOT"
            onOpenCheck={openKitchenCheck}
            onNewUnread={() => showToast.info(t('notifications.new_food_ready'))}
          />
        </div>
      </aside>
      </div>
    </div>
  );
}
