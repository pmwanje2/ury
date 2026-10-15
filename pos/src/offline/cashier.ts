import { logoutSession, clearAppStorage } from '@ury/core';
import { t } from '../i18n';
import { v4 as uuid } from 'uuid';
import { OfflineOutbox, canCaptureOffline, withEmptyOutbox, purgeEmptyOutbox, type OfflineProfile, type OfflineUser, type OfflineOrder, type OfflineEntry, type OfflineSettlement } from './outbox';
import { gatewayStatusOf } from './gateway';

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
    ${kind === 'bill' ? `<p>${escape(t('payment.total'))}: ${escape(lastRound.expected_total)}</p><p>${escape(entry.settlement?.mode_of_payment)} ${escape(entry.settlement?.reference)}</p>` : ''}
    <p>${escape(t('offline.captured'))}: ${escape(lastRound.captured_at)}</p><p>${escape(t('offline.no_fdn'))}</p></body></html>`);
  popup.document.close(); popup.focus(); popup.print(); return true;
}

export async function assertOfflineActionAllowed(action: string): Promise<void> {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    throw new Error(t('offline.refused', { action }));
  }
}

export async function clearCashierStorage(logout = false, token?: string): Promise<void> {
  await withEmptyOutbox(async () => {
    if (logout) await logoutSession(token);
    await purgeEmptyOutbox();
    await clearAppStorage(); // O3 owns service-worker/cache purge through this existing seam.
  });
}

export function cashierOutbox(profile: OfflineProfile, user: OfflineUser): OfflineOutbox {
  let users = queues.get(profile); if (!users) { users = new WeakMap(); queues.set(profile, users); }
  let queue = users.get(user); if (!queue) { queue = new OfflineOutbox({ profile, user, print: stationPrint }); users.set(user, queue); }
  return queue;
}

export async function submitOrderOfflineAware<T>({ profile, user, order, total, onlineSubmit, clientOrderId }:
  { profile: OfflineProfile; user: OfflineUser; order: OfflineOrder; total: number;
    onlineSubmit: (ids?: { clientOrderId: string; roundId: string }) => Promise<T>; clientOrderId?: string }) {
  const queue = cashierOutbox(profile, user);
  if (typeof navigator !== 'undefined' && navigator.onLine === false || !queue.reachable || clientOrderId) {
    const entry = await queue.capture(order, total, clientOrderId);
    return { offline: true as const, entry };
  }
  // The id goes out on the live write. A gateway timeout after the ERP saved
  // the order is replayed with the same id, so the server returns that order.
  const queueable = canCaptureOffline(profile, user) && !order.invoice && !order.last_invoice;
  const ids = queueable ? { clientOrderId: uuid(), roundId: uuid() } : undefined;
  try {
    return { offline: false as const, response: await onlineSubmit(ids) };
  } catch (error) {
    if (!ids || !gatewayStatusOf(error)) throw error;
    queue.reachable = false;
    const entry = await queue.capture(order, total, undefined, ids);
    return { offline: true as const, entry };
  }
}

function offlineCashOrMobile(mode: string, reference?: string): OfflineSettlement['kind'] | null {
  if (/^cash$/i.test(mode)) return 'cash';
  if (/mobile.?money|momo|m-?pesa/i.test(mode) && !!reference?.trim()) return 'mobile-money';
  return null;
}

export async function submitSettlementOfflineAware<T>({ profile, user, invoice, customer, table, cashier, owner,
  clientOrderId, amount, due, modeOfPayment, reference, discount, orderType, onlineSettle }: {
  profile: OfflineProfile; user: OfflineUser; invoice: string; customer?: string; table?: string | null;
  cashier?: string; owner?: string; clientOrderId?: string | null; amount: number; due: number; modeOfPayment: string;
  reference?: string; discount?: number; orderType?: string;
  onlineSettle: (ids?: { clientOrderId: string; roundId: string }) => Promise<T>;
}) {
  const queue = cashierOutbox(profile, user);
  const kind = offlineCashOrMobile(modeOfPayment, reference);
  // A short named-account settlement (#44) leaves a balance owed. It is not a
  // cash settlement and stays online-only, so the tender must cover the bill.
  const paidInFull = Number.isFinite(due) && amount >= due - 0.005;
  const queueable = !!kind && paidInFull && canCaptureOffline(profile, user) && !(discount && discount > 0);
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
    const settlement: OfflineSettlement = { kind: kind!, mode_of_payment: modeOfPayment, amount, reference };
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
