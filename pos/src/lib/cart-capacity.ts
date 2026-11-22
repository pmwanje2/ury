import type { ItemAvailability } from './availability-api';

// Cart headroom is display-only and uses the screen's menu batch snapshot.
// The order endpoint remains the authority for changes after that snapshot.
/**
 * Total quantity of `item_code` already present across every cart line
 * (every variant/addon combination is a separate line, but they all draw
 * on the same finished-item capacity).
 */
export const cartQtyForItem = <T extends { item: string; quantity: number }>(
  lines: T[],
  item_code: string,
): number => lines.filter((line) => line.item === item_code).reduce((sum, line) => sum + line.quantity, 0);

/**
 * Remaining headroom for `item_code`: how many more units of it this cart
 * could add before hitting the last-known available quantity. `undefined`
 * means "unknown -- do not gate" (no availability data fetched yet, lookup
 * failed, or availability isn't tracked for this item).
 */
export const remainingHeadroom = (
  availability: ItemAvailability | undefined,
  currentCartQty: number,
): number | undefined => {
  if (!availability) return undefined;
  if (!availability.sellable) return 0;
  // `available_qty == null` means "unconstrained" (e.g. an "Always
  // Available" override) -- do not gate on it, same as unknown/unfetched.
  if (availability.available_qty == null) return undefined;
  return availability.available_qty - currentCartQty;
};
