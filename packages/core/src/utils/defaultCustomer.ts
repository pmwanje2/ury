/**
 * Per-order-type default customer resolution (client mirror of
 * `ury.ury_pos.default_customer.resolve_default_customer`).
 *
 * The backend sends `order_type_customer_map` and `default_customer` on the POS
 * profile payload. Resolution order for a NEW order:
 *   1. the entry for the order type
 *   2. the profile-level default customer
 *   3. null
 *
 * Aggregator orders are never resolved (the aggregator flow owns its customer).
 * Pure and synchronous: no network, so it works offline from the cached profile.
 */

export interface DefaultCustomerEntry {
  customer: string;
  customer_name?: string | null;
  mobile_number?: string | null;
}

export interface DefaultCustomerConfig {
  order_type_customer_map?: Record<string, DefaultCustomerEntry> | null;
  default_customer?: DefaultCustomerEntry | null;
}

export interface ResolvedDefaultCustomer {
  id: string;
  name: string;
  phone: string;
}

const AGGREGATORS = 'Aggregators';

function toCustomer(entry: DefaultCustomerEntry): ResolvedDefaultCustomer {
  return {
    id: entry.customer,
    name: entry.customer_name || entry.customer,
    phone: entry.mobile_number || '',
  };
}

export function resolveDefaultCustomer(
  orderType: string | null | undefined,
  config: DefaultCustomerConfig | null | undefined
): ResolvedDefaultCustomer | null {
  if (!config || !orderType || orderType === AGGREGATORS) return null;

  const entry = config.order_type_customer_map?.[orderType] ?? config.default_customer ?? null;
  return entry?.customer ? toCustomer(entry) : null;
}

/**
 * True when a cached profile predates the default-customer keys, so the client
 * should refetch instead of treating the missing keys as "nothing configured".
 */
export function profileLacksDefaultCustomerConfig(profile: object | null | undefined): boolean {
  return !profile || !('order_type_customer_map' in profile);
}

/**
 * True when `customer` is any default customer the profile resolves: an
 * order-type entry or the profile-level default. Such customers are not named
 * accounts, whatever the order's type.
 */
export function isConfiguredDefaultCustomer(
  customer: string | null | undefined,
  config: DefaultCustomerConfig | null | undefined
): boolean {
  if (!customer || !config) return false;
  if (config.default_customer?.customer === customer) return true;
  return Object.values(config.order_type_customer_map ?? {}).some((entry) => entry?.customer === customer);
}
