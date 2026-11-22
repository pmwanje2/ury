import { useCallback, useEffect, useRef, useState } from 'react';
import { getMenuAvailability, isUnavailable, type ItemAvailability, type MenuAvailability } from './availability-api';
import { getRealtimeSocket } from './realtime';

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
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const refetchAll = useCallback(() => refreshRef.current(), []);

  useEffect(() => {
    const codes = JSON.parse(codesKey) as string[];
    if (!branch || !company || !codes.length) return;

    let active = true;
    let inFlight: Promise<void> | undefined;
    const refresh = () => {
      if (inFlight) return inFlight;
      inFlight = getMenuAvailability(branch, company, codes)
        .then((items) => { if (active) setSnapshot({ scope, items }); })
        .catch(() => { /* Unknown on first load; keep the last successful refresh. */ })
        .finally(() => { inFlight = undefined; });
      return inFlight;
    };
    const refreshIfVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };

    refreshRef.current = refresh;
    void refresh();
    const timer = setInterval(refreshIfVisible, 60_000);
    document.addEventListener('visibilitychange', refreshIfVisible);
    const channel = `menu_availability_update_${branch}`;
    let unsubscribe: (() => void) | undefined;
    void getRealtimeSocket().then((socket) => {
      if (!active) return;
      socket.on(channel, refreshIfVisible);
      unsubscribe = () => { socket.off(channel, refreshIfVisible); };
    }).catch(() => { /* The periodic refresh does not depend on the socket. */ });

    return () => {
      active = false;
      refreshRef.current = async () => {};
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshIfVisible);
      unsubscribe?.();
    };
  }, [branch, company, codesKey, scope]);

  return { availabilityMap: snapshot?.scope === scope ? snapshot.items : EMPTY_AVAILABILITY, refetchAll };
}
