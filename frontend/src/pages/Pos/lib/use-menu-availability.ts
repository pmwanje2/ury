import { useEffect, useRef, useState } from 'react';
import { getMenuAvailability, type ItemAvailability } from './availability-api';
import { useMenuAvailabilityChannel } from './realtime';

export type AvailabilityTier = 0 | 1 | 2;

/**
 * Maps an item's availability to a sort tier:
 * - 0: Available / In Stock (sellable && (available_qty > 0 || available_qty == null)) or unknown/fail-open
 * - 1: Sold Out (sellable == false or available_qty <= 0, reason: PLAN_EXHAUSTED or FG_OUT_OF_STOCK)
 * - 2: Temporarily Unavailable / Other un-sellable reasons (BLOCKING_COMPONENT, MISSING_BOM, NOT_PRODUCED, etc.)
 */
export function getAvailabilityTier(availability?: ItemAvailability | null): AvailabilityTier {
  if (!availability) {
    return 0;
  }

  const isUnavailable =
    !availability.sellable ||
    (availability.available_qty != null && availability.available_qty <= 0);

  if (!isUnavailable) {
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

const EMPTY: Record<string, ItemAvailability> = {};

export function useMenuAvailability(
  items: Array<{ item: string }>,
  branch?: string,
  company?: string
) {
  const itemsKey = JSON.stringify([...new Set(items.map(row => row.item).filter(Boolean))].sort());
  const scope = JSON.stringify([branch, company, itemsKey]);
  const [snapshot, setSnapshot] = useState<{ scope: string; items: Record<string, ItemAvailability> }>();
  const refreshRef = useRef<() => void>(() => {});
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const codes = JSON.parse(itemsKey) as string[];
    if (!branch || !company || !codes.length) { setLoading(false); return; }
    let active = true;
    let inFlight = false;
    let eventTimer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      if (inFlight) return;
      inFlight = true;
      void getMenuAvailability(branch, company, codes).then(items => {
        if (active) setSnapshot({ scope, items });
      }).catch(() => { /* Keep the last successful snapshot. */ }).finally(() => {
        inFlight = false;
        if (active) setLoading(false);
      });
    };
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    refreshRef.current = () => {
      clearTimeout(eventTimer);
      eventTimer = setTimeout(visible, 500);
    };
    setLoading(true);
    refresh();
    const timer = setInterval(visible, 60_000);
    document.addEventListener('visibilitychange', visible);
    return () => {
      active = false;
      clearInterval(timer);
      clearTimeout(eventTimer);
      document.removeEventListener('visibilitychange', visible);
      refreshRef.current = () => {};
    };
  }, [itemsKey, scope, branch, company]);
  useMenuAvailabilityChannel(branch, () => refreshRef.current());
  return { availabilityMap: snapshot?.scope === scope ? snapshot.items : EMPTY, loading };
}
