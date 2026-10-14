import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { useOutbox } from '../offline/use-outbox';
import { t } from '../i18n';

export default function OfflineBanner() {
  const { posProfile } = usePOSStore(); const { user } = useRootStore();
  const { rows, replaying, offline } = useOutbox(posProfile, user, true);
  const pending = rows.filter((row) => row.state === 'pending').length;
  const review = rows.filter((row) => row.state === 'review');
  if (!rows.length && !offline) return null;
  return <section role="status" data-testid="offline-banner" className="bg-amber-100 border-b border-amber-300 px-4 py-2 text-sm text-amber-950">
    <strong>{t(replaying ? 'offline.replaying' : offline ? 'offline.capture' : 'offline.outbox')}</strong>
    <span className="ms-2">{t('offline.pending', { count: pending })}{review.length ? ` · ${t('offline.review_count', { count: review.length })}` : ''}</span>
    {review.length > 0 && <ul className="mt-1 list-disc ps-5">{review.map((row) => <li key={row.sequence}>{row.reason}</li>)}</ul>}
  </section>;
}
