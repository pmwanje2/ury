import { call } from '@ury/core'
import { t } from '../i18n'

const M = 'ury.ury.api.ury_availability'

/**
 * Thin, read-only adapter over V3-44's `get_item_availability` whitelisted
 * endpoint (`ury/ury/api/ury_availability.py`). Mirrors this app's existing
 * `M.<method>` dotted-path convention (see `api.ts`).
 *
 * This module never re-derives availability math client-side — every field
 * below is passed through verbatim from the server response.
 *
 * Cache-invalidation / TTL policy (V3-40 design note: "Cached availability
 * may drive menus, dashboards, and UX, but order acceptance must re-check
 * transactionally"):
 *   - The in-memory cache here is for MENU DISPLAY ONLY (dimming/greying an
 *     item, showing a "Sold out" badge, etc).
 *   - Entries expire after `CACHE_TTL_MS` (30s) and are also invalidated
 *     manually via `invalidateAvailabilityCache()`.
 *   - `addItems`/order-confirmation code must NEVER read from this cache to
 *     decide whether an item can actually be added — it must call
 *     `getItemAvailability(..., { skipCache: true })` at that decision
 *     point, or rely on the backend's own transactional rejection.
 *
 * `OrderingContext` (see `api.ts`) exposes both `restaurant` (used here as
 * `branch`) and `company`, sourced server-side from the ordering profile's
 * branch -> company lookup (see self_ordering.py's
 * `_ordering_context_response`). `get_item_availability` requires `company`
 * and fails closed (throws) without it — callers here should still treat a
 * thrown/failed lookup as "unknown availability" (soft-fail, do not block
 * the whole menu) rather than as CONFIGURATION_ERROR, e.g. for sessions
 * whose branch has no company mapped in `Branch`.
 */

export type AvailabilityReasonCode =
  | 'AVAILABLE'
  | 'NOT_PRODUCED'
  | 'PLAN_EXHAUSTED'
  | 'FG_OUT_OF_STOCK'
  | 'NO_ACTIVE_PLAN'
  | 'BLOCKING_COMPONENT'
  | 'MISSING_BOM'
  | 'MISSING_PRODUCTION_UNIT'
  | 'PRODUCTION_UNIT_DISABLED'
  | 'MISSING_DEPARTMENT'
  | 'DEPARTMENT_DISABLED'
  | 'CONFIGURATION_ERROR'

export interface ItemAvailability {
  item_code: string
  sellable: boolean
  available_qty: number
  production_policy: string | null
  company: string
  branch: string
  department: string | null
  production_unit: string | null
  warehouse: string | null
  plan_qty: number | null
  plan_remaining: number | null
  fg_available: number | null
  max_producible: number | null
  blocking_component: string | null
  reason_code: AvailabilityReasonCode | string
  as_of: string
}

export interface GetItemAvailabilityParams {
  item_code: string
  branch: string
  company: string
  department?: string
}

/** Reason code -> catalog key. Codes taken verbatim from
 * `ury/ury/api/ury_availability.py` — no invented codes. */
const AVAILABILITY_REASON_KEYS: Record<string, string> = {
  AVAILABLE: 'availability.available',
  NOT_PRODUCED: 'availability.not_today',
  PLAN_EXHAUSTED: 'availability.sold_out',
  FG_OUT_OF_STOCK: 'availability.sold_out',
  NO_ACTIVE_PLAN: 'availability.not_today',
  BLOCKING_COMPONENT: 'availability.temporarily',
  MISSING_BOM: 'availability.temporarily',
  MISSING_PRODUCTION_UNIT: 'availability.temporarily',
  PRODUCTION_UNIT_DISABLED: 'availability.temporarily',
  MISSING_DEPARTMENT: 'availability.temporarily',
  DEPARTMENT_DISABLED: 'availability.temporarily',
  CONFIGURATION_ERROR: 'availability.temporarily',
}

/** Reason code -> user-facing message in the guest's language, read when shown. */
export const AVAILABILITY_REASON_MESSAGES: Record<string, string> = Object.defineProperties(
  {},
  Object.fromEntries(
    Object.entries(AVAILABILITY_REASON_KEYS).map(([code, key]) => [code, { enumerable: true, get: () => t(key) }]),
  ),
)

export function getAvailabilityMessage(reasonCode: string | null | undefined): string {
  const key = reasonCode ? AVAILABILITY_REASON_KEYS[reasonCode] : undefined
  return t(key ?? 'availability.unavailable')
}

interface FrappeResponse<T> {
  message: T
}

// --- display-only cache -----------------------------------------------

const CACHE_TTL_MS = 30_000

interface CacheEntry {
  value: ItemAvailability
  expiresAt: number
}

const cache = new Map<string, CacheEntry>()

function buildCacheKey(params: GetItemAvailabilityParams): string {
  return [params.item_code, params.branch, params.company, params.department || ''].join('::')
}

export function invalidateAvailabilityCache(params?: GetItemAvailabilityParams) {
  if (!params) {
    cache.clear()
    return
  }
  cache.delete(buildCacheKey(params))
}

interface GetItemAvailabilityOptions {
  /** Bypass the display cache entirely — required at order-confirmation time. */
  skipCache?: boolean
}

export async function getItemAvailability(
  params: GetItemAvailabilityParams,
  options: GetItemAvailabilityOptions = {},
): Promise<ItemAvailability> {
  const key = buildCacheKey(params)

  if (!options.skipCache) {
    const cached = cache.get(key)
    if (cached && cached.expiresAt > Date.now()) {
      return cached.value
    }
  }

  const response = await call.get<FrappeResponse<ItemAvailability>>(`${M}.get_item_availability`, {
    item_code: params.item_code,
    branch: params.branch,
    company: params.company,
    department: params.department,
  })
  const value = response.message
  cache.set(key, { value, expiresAt: Date.now() + CACHE_TTL_MS })
  return value
}
