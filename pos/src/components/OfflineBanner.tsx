import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { useOutbox } from '../offline/use-outbox';
import { t } from '../i18n';
import { useState } from 'react';
import { Button, showToast } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { canCaptureOffline, type OfflineEntry, type OfflineOutbox } from '../offline/outbox';

function PendingOrder({ row, queue, modes, disabled }: { row: OfflineEntry; queue: OfflineOutbox; modes: string[]; disabled: boolean }) {
  const [busy, setBusy] = useState(false);
  const [reference, setReference] = useState('');
  const cash = modes.find((mode) => /^cash$/i.test(mode));
  const mobile = modes.filter((mode) => /mobile.?money|momo|m-?pesa/i.test(mode));
  const [mobileMode, setMobileMode] = useState(mobile[0] || '');
  const total = row.rounds[row.rounds.length - 1].expected_total;
  const action = async (run: () => Promise<unknown>) => {
    setBusy(true);
    try { await run(); } catch (error) { showToast.error(error instanceof Error ? error.message : t('errors.payment_failed')); }
    finally { setBusy(false); }
  };
  return <li className="border-t border-amber-300 py-2" data-client-order={row.client_order_id}>
    <span>{row.order.customer || row.order.table} · {row.client_order_id.slice(0, 8)} · {t('payment.total')}: {total}</span>
    {row.rounds.some((round) => !round.offline_printed) && <Button size="sm" disabled={disabled || busy} onClick={() => action(() => queue.reprint(row.client_order_id, 'kot'))}>{t('offline.reprint_kot')}</Button>}
    {row.settlement ? <span className="ms-2">{t('offline.settled')} · {row.settlement.mode_of_payment}
      {!row.bill_printed && <Button size="sm" disabled={disabled || busy} onClick={() => action(() => queue.reprint(row.client_order_id, 'bill'))}>{t('offline.reprint_bill')}</Button>}
    </span> : <div className="flex flex-wrap gap-2 mt-1">
      {cash && <Button size="sm" data-testid="settle-cash" disabled={disabled || busy || row.next_round > 0} onClick={() => action(() => queue.settle(row.client_order_id, { kind: 'cash', mode_of_payment: cash, amount: total }))}>{t('offline.cash')}</Button>}
      {mobile.length > 0 && <>
        <select aria-label={t('offline.mobile_mode')} value={mobileMode} disabled={disabled || busy} onChange={(event) => setMobileMode(event.target.value)}>{mobile.map((mode) => <option key={mode}>{mode}</option>)}</select>
        <input aria-label={t('offline.reference')} placeholder={t('offline.reference')} value={reference} disabled={disabled || busy} onChange={(event) => setReference(event.target.value)} />
        <Button size="sm" disabled={disabled || busy || !reference.trim() || row.next_round > 0} onClick={() => action(() => queue.settle(row.client_order_id, { kind: 'mobile-money', mode_of_payment: mobileMode, amount: total, reference }))}>{t('offline.mobile')}</Button>
      </>}
    </div>}
  </li>;
}

export default function OfflineBanner() {
  const { posProfile, paymentModes } = usePOSStore(); const { user } = useRootStore();
  const { rows, replaying, offline, queue } = useOutbox(posProfile, user, true);
  const pending = rows.filter((row) => row.state === 'pending').length;
  const review = rows.filter((row) => row.state === 'review');
  if (!rows.length && !offline) return null;
  return <section role="status" data-testid="offline-banner" className="bg-amber-100 border-b border-amber-300 px-4 py-2 text-sm text-amber-950">
    <strong>{t(replaying ? 'offline.replaying' : offline ? 'offline.capture' : 'offline.outbox')}</strong>
    <span className="ms-2">{t('offline.pending', { count: pending })}{review.length ? ` · ${t('offline.review_count', { count: review.length })}` : ''}</span>
    {offline && <p>{t('offline.refusals')}</p>}
    {queue && pending > 0 && <details open className="max-h-48 overflow-y-auto"><summary>{t('offline.outbox')}</summary><ul>{rows.filter((row) => row.state === 'pending').map((row) => <PendingOrder key={row.sequence} row={row} queue={queue} modes={paymentModes} disabled={replaying || !canCaptureOffline(posProfile, user)} />)}</ul></details>}
    {review.length > 0 && <ul className="mt-1 list-disc ps-5">{review.map((row) => <li key={row.sequence}>{row.client_order_id.slice(0, 8)} · {parseFrappeError({ _server_messages: row.reason }, row.reason || '')}</li>)}</ul>}
  </section>;
}
