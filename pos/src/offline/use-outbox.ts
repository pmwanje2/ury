import { useEffect, useState } from 'react';
import { cashierOutbox } from './cashier';
import { canCaptureOffline, OUTBOX_CHANGED, type OfflineEntry, type OfflineProfile, type OfflineUser } from './outbox';

export function useOutbox(profile?: OfflineProfile | null, user?: OfflineUser | null, replay = false) {
  const queue = profile && user && profile.ury_offline_capture ? cashierOutbox(profile, user) : null;
  const provisional = Boolean((user as { provisional?: boolean } | null | undefined)?.provisional);
  const provisionalOffline = Boolean(queue && !queue.sessionBlocked && provisional && !queue.verified);
  const computeOffline = () =>
    !queue?.sessionBlocked &&
    ((typeof navigator !== 'undefined' && navigator.onLine === false) ||
      queue?.reachable === false ||
      provisionalOffline);
  const [state, setState] = useState(() => ({
    rows: [] as OfflineEntry[],
    replaying: false,
    offline: computeOffline(),
  }));
  useEffect(() => {
    let disposed = false;
    const refresh = async () => {
      const rows = queue ? await queue.list() : [];
      if (!disposed) {
        setState({
          rows,
          replaying: !!queue?.replaying,
          offline:
            !queue?.sessionBlocked &&
            (navigator.onLine === false || queue?.reachable === false || (provisional && !queue?.verified)),
        });
      }
    };
    const wake = () => { void refresh(); if (replay && navigator.onLine !== false && canCaptureOffline(profile, user)) void queue?.replay(); };
    const update = () => { void refresh(); };
    if (!queue) return;
    window.addEventListener(OUTBOX_CHANGED, update); window.addEventListener('offline', update); window.addEventListener('online', wake);
    const timer = replay ? window.setInterval(wake, 5000) : undefined;
    if (replay) wake(); else update();
    return () => { disposed = true; window.removeEventListener(OUTBOX_CHANGED, update); window.removeEventListener('offline', update); window.removeEventListener('online', wake); if (timer) window.clearInterval(timer); };
  }, [queue, profile, user, replay, provisional]);
  return {
    ...state,
    offline: !queue?.sessionBlocked && (state.offline || provisionalOffline),
    rows: queue ? state.rows.filter((row) => row.owner === user?.name && row.profile === profile?.name) : [],
    queue,
  };
}
