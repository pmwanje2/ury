import { clearAppStorage, logout, purgeOfflineSession } from '@ury/core';
import { t } from '../i18n';
import { v4 as uuid } from 'uuid';
import {
  OfflineOutbox,
  canCaptureOfflineOrder,
  canSettleOffline,
  openTableRow,
  exceedsOfflineTenderLimit,
  resolveOfflineModeConfig,
  withEmptyOutbox,
  purgeEmptyOutbox,
  type OfflineProfile,
  type OfflineUser,
  type OfflineOrder,
  type OfflineEntry,
  type OfflineSettlement,
} from './outbox';
import { gatewayStatusOf } from './gateway';
import { readOpenTableCheck } from './open-table-checks';
import { triggerPostReplayFiscalPrint } from '../lib/efris-print';

const queues = new WeakMap<OfflineProfile, WeakMap<OfflineUser, OfflineOutbox>>();
const escape = (value: unknown) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!);

function stationPrint(entry: OfflineEntry, kind: 'kot' | 'bill'): boolean {
  const popup = window.open('', '_blank');
  if (!popup) return false;
  popup.opener = null;
  const lastRound = entry.rounds[entry.rounds.length - 1];
  const rounds = kind === 'kot' ? [lastRound] : entry.rounds;
  const items = rounds.flatMap((round) => round.items).map((item) =>
    `<li>${escape(item.qty)} × ${escape(item.item_name)} ${escape(item.comment)}</li>`).join('');
  const title = t(kind === 'kot' ? 'offline.kot' : 'offline.provisional');
  popup.document.write(`<html><head><title>${title}</title><style>body{font:16px sans-serif;max-width:360px;margin:24px}li{margin:12px 0}</style></head><body>
    <h1>${title}</h1><p>${escape(entry.order.table || entry.order.order_type)} · ${escape(entry.order.customer)}</p>
    <p>${escape(t('offline.client_order'))}: ${escape(entry.client_order_id)}</p><ul>${items}</ul>
    ${kind === 'bill' ? `<p>${escape(t('payment.total'))}: ${escape(lastRound.expected_total)}</p><p>${escape(entry.settlement?.mode_of_payment)} ${escape(entry.settlement?.reference)}</p>${entry.settlement?.room_number ? `<p>${escape(t('payment.room_folio'))}: ${escape(entry.settlement.room_number)}</p>` : ''}` : ''}
    <p>${escape(t('offline.captured'))}: ${escape(lastRound.captured_at)}</p><p>${escape(t('offline.no_fdn'))}</p></body></html>`);
  popup.document.close(); popup.focus(); popup.print(); return true;
}

export async function assertOfflineActionAllowed(action: string): Promise<void> {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    throw new Error(t('offline.refused', { action }));
  }
}

// Sign-out is refused while the outbox holds pending or Needs review orders.
export async function clearCashierStorage(signOut = false): Promise<void> {
  await withEmptyOutbox(async () => {
    if (signOut && await logout() === false) throw new Error(t('errors.failed_logout'));
    await purgeEmptyOutbox();
    if (!signOut) {
      clearAppStorage();
      await purgeOfflineSession();
    }
  });
}

export function cashierOutbox(profile: OfflineProfile, user: OfflineUser): OfflineOutbox {
  let users = queues.get(profile); if (!users) { users = new WeakMap(); queues.set(profile, users); }
  let queue = users.get(user);
  if (!queue) {
    queue = new OfflineOutbox({
      profile,
      user,
      print: stationPrint,
      onSettled: async ({ invoice, entry }) => {
        if (entry.settlement?.fiscal_receipt_requested === 0) return;
        await triggerPostReplayFiscalPrint({ invoice, posProfile: profile });
      },
    });
    users.set(user, queue);
  }
  return queue;
}

export async function submitOrderOfflineAware<T>({ profile, user, order, total, onlineSubmit, clientOrderId }:
  { profile: OfflineProfile; user: OfflineUser; order: OfflineOrder; total: number;
    onlineSubmit: (ids?: { clientOrderId: string; roundId: string }) => Promise<T>; clientOrderId?: string }) {
  const queue = cashierOutbox(profile, user);
  queue.assertSessionAllowed();
  if (typeof navigator !== 'undefined' && navigator.onLine === false || !queue.reachable || clientOrderId) {
    const entry = await queue.capture(order, total, clientOrderId);
    return { offline: true as const, entry };
  }
  // The id goes out on the live write. A free table keeps that id if the
  // gateway drops the ack. An occupied table reuses its open check id.
  const queueable = canCaptureOfflineOrder(profile, user, order.order_type) && !order.invoice && !order.last_invoice;
  const occupied = queueable && order.table ? await openClientForTable(queue, user.name, order.table) : undefined;
  const ids = queueable ? { clientOrderId: occupied || uuid(), roundId: uuid() } : undefined;
  try {
    return { offline: false as const, response: await onlineSubmit(ids) };
  } catch (error) {
    if (!ids || !gatewayStatusOf(error)) throw error;
    queue.reachable = false;
    // Same client and round id as the live write, free or occupied table.
    const entry = await queue.capture(order, total, undefined, ids);
    return { offline: true as const, entry };
  }
}

async function openClientForTable(queue: OfflineOutbox, user: string, table: string): Promise<string | undefined> {
  const local = openTableRow(await queue.list(), table);
  return local?.client_order_id || readOpenTableCheck(user, table)?.clientOrderId;
}

export function resolveOfflineTender(
  profile: OfflineProfile,
  modeOfPayment: string,
  amount: number,
  reference?: string,
): OfflineSettlement['kind'] | null {
  const cfg = resolveOfflineModeConfig(profile, modeOfPayment);
  if (!cfg) return null;
  if (cfg.requires_reference && !reference?.trim()) return null;
  if (exceedsOfflineTenderLimit(profile, amount)) return null;
  if (cfg.type === 'Cash') return 'cash';
  return cfg.requires_reference ? 'mobile-money' : 'offline';
}

export async function submitSettlementOfflineAware<T>({ profile, user, invoice, customer, table, cashier, owner,
  clientOrderId, amount, due, modeOfPayment, reference, referenceDate, roomNumber, discount, tip, orderType, fiscalReceiptRequested = 1, onlineSettle }: {
  profile: OfflineProfile; user: OfflineUser; invoice: string; customer?: string; table?: string | null;
  cashier?: string; owner?: string; clientOrderId?: string | null; amount: number; due: number; modeOfPayment: string;
  reference?: string; referenceDate?: string; roomNumber?: string; discount?: number; tip?: number; orderType?: string;
  fiscalReceiptRequested?: 0 | 1;
  onlineSettle: (ids?: { clientOrderId: string; roundId: string }) => Promise<T>;
}) {
  const queue = cashierOutbox(profile, user);
  queue.assertSessionAllowed();
  const kind = resolveOfflineTender(profile, modeOfPayment, amount, reference);
  // A short named-account settlement (#44) leaves a balance owed. It is not a
  // cash settlement and stays online-only, so the tender must cover the bill.
  const paidInFull = Number.isFinite(due) && amount >= due - 0.005;
  // Discounts, tips, and over-HALO tenders stay online: the outbox replays none of them.
  const haloLimit = Number(
    (profile as { payment_mode_meta?: Record<string, { halo_max_amount?: number }> })
      .payment_mode_meta?.[modeOfPayment]?.halo_max_amount,
  ) || 0;
  const exceedsHalo = haloLimit > 0 && amount > haloLimit + 1e-6;
  const queueable = !!kind && paidInFull && canSettleOffline(profile, user) && !(discount && discount > 0)
    && !(tip && tip > 0) && !exceedsHalo;
  if (!queueable) return { offline: false as const, response: await onlineSettle() };
  const existing = clientOrderId
    ? (await queue.list()).find((row) => row.client_order_id === clientOrderId && row.state === 'pending')
    : undefined;
  const ids = {
    clientOrderId: existing?.client_order_id || clientOrderId || uuid(),
    roundId: existing?.rounds[existing.rounds.length - 1]?.round_id || uuid(),
  };
  try {
    return { offline: false as const, response: await onlineSettle(ids) };
  } catch (error) {
    if (!gatewayStatusOf(error)) throw error;
    queue.reachable = false;
    const cleanedRoom = roomNumber?.trim() || undefined;
    const settlement: OfflineSettlement = {
      kind: kind!, mode_of_payment: modeOfPayment, amount, reference, fiscal_receipt_requested: fiscalReceiptRequested,
      ...(referenceDate ? { reference_date: referenceDate } : {}),
      ...(cleanedRoom ? { room_number: cleanedRoom } : {}),
    };
    if (existing && !existing.settlement && !existing.next_round) {
      const entry = await queue.settle(existing.client_order_id, settlement);
      return { offline: true as const, entry };
    }
    const entry = await queue.queueGatewaySettlement({
      clientOrderId: ids.clientOrderId, roundId: ids.roundId, invoice, customer, table, cashier, owner, orderType, settlement,
    });
    return { offline: true as const, entry };
  }
}

export const settleOfflineOrder = ({ profile, user, clientOrderId, kind, modeOfPayment, amount, reference }:
  { profile: OfflineProfile; user: OfflineUser; clientOrderId: string; kind: OfflineSettlement['kind']; modeOfPayment: string; amount: number; reference?: string }) =>
  cashierOutbox(profile, user).settle(clientOrderId, { kind, mode_of_payment: modeOfPayment, amount, reference });
