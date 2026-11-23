import { useCallback, useEffect, useRef, useState } from 'react';
import { getMenuAvailability, isUnavailable, type ItemAvailability, type MenuAvailability } from './availability-api';
import { getRealtimeSocket } from './realtime';
import { useOnline } from '../offline/use-online';

export type AvailabilityTier = 0 | 1 | 2;

/**
 * Maps an item's availability to a sort tier:
 * - 0: Available / In Stock (sellable && (available_qty > 0 || available_qty == null)) or unknown/fail-open
 * - 1: Sold Out (sellable == false or available_qty <= 0, reason: PLAN_EXHAUSTED or FG_OUT_OF_STOCK)
 * - 2: Temporarily Unavailable / Other un-sellable reasons (BLOCKING_COMPONENT, MISSING_BOM, NOT_PRODUCED, etc.)
 */
export function getAvailabilityTier(availability?: ItemAvailability | null): AvailabilityTier {
  if (!availability) {
    // Fail-open: items without loaded/known availability stay at top so they aren't hidden
    return 0;
  }

  if (!isUnavailable(availability)) {
    return 0;
  }

  const reasonCode = availability.reason_code;
  if (reasonCode === 'PLAN_EXHAUSTED' || reasonCode === 'FG_OUT_OF_STOCK' ||
      reasonCode === 'SOLD_OUT_MANUAL' || reasonCode === 'COUNT_EXHAUSTED') {
    return 1;
  }

  return 2;
}

/**
 * Sorts menu items by availability tier:
 * Tier 0 (Available) -> Tier 1 (Sold Out) -> Tier 2 (Temporarily Unavailable).
 * Within each tier, items maintain alphabetical order by name.
 */
export function sortMenuItemsByAvailability<T extends { item: string; name: string }>(
  items: T[],
  availabilityMap: Record<string, ItemAvailability>
): T[] {
  return [...items].sort((a, b) => {
    const availA = availabilityMap[a.item];
    const availB = availabilityMap[b.item];
    const tierA = getAvailabilityTier(availA);
    const tierB = getAvailabilityTier(availB);

    if (tierA !== tierB) {
      return tierA - tierB;
    }

    return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
  });
}

const EMPTY_AVAILABILITY: MenuAvailability = {};

export function useMenuAvailability(
  items: Array<{ item: string }>,
  branch?: string,
  company?: string,
) {
  const codesKey = JSON.stringify([...new Set(items.map(({ item }) => item).filter(Boolean))].sort());
  const scope = JSON.stringify([branch, company, codesKey]);
  const [snapshot, setSnapshot] = useState<{ scope: string; items: MenuAvailability }>();
  // The shown counts are not known to be current: a refresh failed, or the device went offline since.
  const [unconfirmed, setUnconfirmed] = useState(false);
  const online = useOnline();
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const refetchAll = useCallback(() => refreshRef.current(), []);

  useEffect(() => {
    const codes = JSON.parse(codesKey) as string[];
    if (!branch || !company || !codes.length) return;

    let active = true;
    let inFlight: Promise<void> | undefined;
    // Realtime updates are missed while offline: only a read sent after the last outage confirms the counts.
    let outages = 0;
    const refresh = () => {
      if (inFlight) return inFlight;
      const sentAfter = outages;
      inFlight = getMenuAvailability(branch, company, codes)
        .then((items) => { if (active) { setSnapshot({ scope, items }); setUnconfirmed(sentAfter !== outages); } })
        // Unknown on first load; otherwise keep the last successful refresh, marked unconfirmed.
        .catch(() => { if (active) setUnconfirmed(true); })
        .finally(() => { inFlight = undefined; });
      return inFlight;
    };
    const refreshIfVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    const refreshAfterFlight = async () => {
      if (inFlight) await inFlight;
      if (active) await refresh();
    };

    refreshRef.current = refreshAfterFlight;
    void refresh();
    const timer = setInterval(refreshIfVisible, 60_000);
    document.addEventListener('visibilitychange', refreshIfVisible);
    const onOffline = () => { outages += 1; setUnconfirmed(true); };
    const onOnline = () => { void refreshAfterFlight(); };
    window.addEventListener('offline', onOffline);
    window.addEventListener('online', onOnline);
    const channel = `menu_availability_update_${branch}`;
    let eventTimer: ReturnType<typeof setTimeout> | undefined;
    const onAvailabilityUpdate = () => {
      clearTimeout(eventTimer);
      eventTimer = setTimeout(() => {
        if (document.visibilityState === 'visible') void refreshAfterFlight();
      }, 500);
    };
    let unsubscribe: (() => void) | undefined;
    void getRealtimeSocket().then((socket) => {
      if (!active) return;
      socket.on(channel, onAvailabilityUpdate);
      unsubscribe = () => { socket.off(channel, onAvailabilityUpdate); };
    }).catch(() => { /* The periodic refresh does not depend on the socket. */ });

    return () => {
      active = false;
      refreshRef.current = async () => {};
      clearInterval(timer);
      clearTimeout(eventTimer);
      document.removeEventListener('visibilitychange', refreshIfVisible);
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('online', onOnline);
      unsubscribe?.();
    };
  }, [branch, company, codesKey, scope]);

  const current = snapshot?.scope === scope;
  return {
    availabilityMap: current ? snapshot.items : EMPTY_AVAILABILITY,
    /** The shown counts are not confirmed (offline, a failed refresh, or not re-read since reconnecting): Simphony's "[N]?". */
    availabilityStale: current && (unconfirmed || !online),
    refetchAll,
  };
}
