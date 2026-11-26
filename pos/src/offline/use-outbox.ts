import { useEffect, useState } from 'react';
import { isFloorSessionConfirmedForTab } from '../lib/floor-tab';
import { cashierOutbox } from './cashier';
import { assertOutboxEmpty, recheckOutboxMembership, canRecoverOfflineOrder, offlineCaptureEnabled, OUTBOX_CHANGED, type OfflineEntry, type OfflineProfile, type OfflineUser } from './outbox';

export function useOutbox(profile?: OfflineProfile | null, user?: OfflineUser | null, replay = false) {
  const queue = profile && user ? cashierOutbox(profile, user) : null;
  const captureEnabled = offlineCaptureEnabled(profile);
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
      const confirmed = () => isFloorSessionConfirmedForTab(user?.name);
      let rows: OfflineEntry[] = [];
      if (queue && confirmed()) {
        rows = await queue.list();
        if (!confirmed() && rows.length) {
          rows = [];
          if (!disposed) recheckOutboxMembership();
        }
      } else {
        // Count across owners/outlets. An unconfirmed tab never needs order contents.
        try { await assertOutboxEmpty(); } catch { if (!disposed) recheckOutboxMembership(); }
      }
      if (!disposed) {
        setState({
          rows,
          replaying: !!queue?.replaying,
          offline:
            !queue?.sessionBlocked &&
            (navigator.onLine === false || queue?.reachable === false || (provisional && !queue?.verified)),
        });
      }
      return rows.length;
    };
    const wake = async () => {
      const count = await refresh();
      const authorized = () => !disposed && isFloorSessionConfirmedForTab(user?.name);
      if (authorized() && (captureEnabled || count > 0) && replay && navigator.onLine !== false && canRecoverOfflineOrder(profile, user)) {
        await queue?.replay(authorized);
      }
    };
    const update = () => { void refresh(); };
    if (!queue) return;
    window.addEventListener(OUTBOX_CHANGED, update); window.addEventListener('offline', update); window.addEventListener('online', wake);
    const timer = replay ? window.setInterval(wake, 5000) : undefined;
    if (replay) wake(); else update();
    return () => { disposed = true; window.removeEventListener(OUTBOX_CHANGED, update); window.removeEventListener('offline', update); window.removeEventListener('online', wake); if (timer) window.clearInterval(timer); };
  }, [queue, profile, user, replay, provisional, captureEnabled]);
  const rows = isFloorSessionConfirmedForTab(user?.name)
    ? state.rows.filter((row) => row.owner === user?.name && row.profile === profile?.name)
    : [];
  return {
    ...state,
    offline: !queue?.sessionBlocked && (state.offline || provisionalOffline),
    rows,
    queue: isFloorSessionConfirmedForTab(user?.name) && (captureEnabled || rows.length) ? queue : null,
  };
}
