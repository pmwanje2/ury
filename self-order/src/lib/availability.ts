import { call } from '@ury/core'
import { t } from '../i18n'

export interface ItemAvailability {
  item_code: string
  sellable: boolean
  /** Null means there is no quantity constraint. */
  available_qty: number | null
  reason_code: string
}

export type MenuAvailability = Record<string, ItemAvailability>

export function isUnavailable(availability?: ItemAvailability | null): boolean {
  return !!availability && (!availability.sellable ||
    (availability.available_qty != null && availability.available_qty <= 0))
}

/** Reason code -> catalog key. Codes taken verbatim from
 * `ury/ury/api/ury_availability.py` — no invented codes. */
const AVAILABILITY_REASON_KEYS: Record<string, string> = {
  ITEM_DISABLED: 'availability.unavailable',
  MENU_ITEM_DISABLED: 'availability.unavailable',
  SOLD_OUT_MANUAL: 'availability.sold_out',
  COUNT_EXHAUSTED: 'availability.sold_out',
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

/** Always live: used for both menu refresh and the pre-submit check. */
export async function getMenuAvailability(session: string): Promise<MenuAvailability> {
  const response = await call.post<{ message: MenuAvailability }>(
    'ury.ury.api.self_ordering.get_customer_menu_availability', { session },
  )
  return response.message
}
