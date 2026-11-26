import { useEffect } from 'react';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { flushPendingFiscalReprints } from '../lib/efris-print';
import OutboxPanel from './OutboxPanel';

export default function OfflineBanner() {
  const { posProfile, currency } = usePOSStore();
  const { user } = useRootStore();
  useEffect(() => {
    if (!posProfile) return;
    const run = () => {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
      void flushPendingFiscalReprints(posProfile);
    };
    window.addEventListener('online', run);
    const timer = window.setInterval(run, 5000);
    run();
    return () => {
      window.removeEventListener('online', run);
      window.clearInterval(timer);
    };
  }, [posProfile]);
  return <OutboxPanel posProfile={posProfile} user={user} currency={currency} />;
}
