import { logoutSession, clearAppStorage } from '@ury/core';
import { t } from '../i18n';
import { OfflineOutbox, withEmptyOutbox, purgeEmptyOutbox, type OfflineProfile, type OfflineUser, type OfflineOrder, type OfflineEntry, type OfflineSettlement } from './outbox';

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
    clearAppStorage(); // O3 owns service-worker/cache purge through this existing seam.
  });
}

export function cashierOutbox(profile: OfflineProfile, user: OfflineUser): OfflineOutbox {
  let users = queues.get(profile); if (!users) { users = new WeakMap(); queues.set(profile, users); }
  let queue = users.get(user); if (!queue) { queue = new OfflineOutbox({ profile, user, print: stationPrint }); users.set(user, queue); }
  return queue;
}

export async function submitOrderOfflineAware<T>({ profile, user, order, total, onlineSubmit, clientOrderId }:
  { profile: OfflineProfile; user: OfflineUser; order: OfflineOrder; total: number; onlineSubmit: () => Promise<T>; clientOrderId?: string }) {
  const queue = cashierOutbox(profile, user);
  if (typeof navigator !== 'undefined' && navigator.onLine === false || !queue.reachable || clientOrderId) {
    const entry = await queue.capture(order, total, clientOrderId);
    return { offline: true as const, entry };
  }
  // Do not turn an ambiguous lost live acknowledgement into a new offline order.
  return { offline: false as const, response: await onlineSubmit() };
}

export const settleOfflineOrder = ({ profile, user, clientOrderId, kind, modeOfPayment, amount, reference }:
  { profile: OfflineProfile; user: OfflineUser; clientOrderId: string; kind: OfflineSettlement['kind']; modeOfPayment: string; amount: number; reference?: string }) =>
  cashierOutbox(profile, user).settle(clientOrderId, { kind, mode_of_payment: modeOfPayment, amount, reference });
