import { useEffect, useState } from 'react';
import { cashierOutbox } from './cashier';
import { canCaptureOffline, OUTBOX_CHANGED, type OfflineEntry, type OfflineProfile, type OfflineUser } from './outbox';

export function useOutbox(profile?: OfflineProfile | null, user?: OfflineUser | null, replay = false) {
  const queue = profile && user ? cashierOutbox(profile, user) : null;
  const [state, setState] = useState({ rows: [] as OfflineEntry[], replaying: false, offline: typeof navigator !== 'undefined' && navigator.onLine === false });
  useEffect(() => {
    let disposed = false;
    const refresh = async () => {
      const rows = queue ? await queue.list() : [];
      if (!disposed) setState({ rows, replaying: !!queue?.replaying, offline: navigator.onLine === false || queue?.reachable === false });
    };
    const wake = () => { void refresh(); if (replay && navigator.onLine !== false && canCaptureOffline(profile, user)) void queue?.replay(); };
    const update = () => { void refresh(); };
    void refresh();
    window.addEventListener(OUTBOX_CHANGED, update); window.addEventListener('offline', update); window.addEventListener('online', wake);
    const timer = replay ? window.setInterval(wake, 5000) : undefined;
    if (replay) wake();
    return () => { disposed = true; window.removeEventListener(OUTBOX_CHANGED, update); window.removeEventListener('offline', update); window.removeEventListener('online', wake); if (timer) window.clearInterval(timer); };
  }, [queue, profile, user, replay]);
  return { ...state, queue };
}
