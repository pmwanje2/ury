import { call } from '@ury/core'

const M = 'ury.ury.api.self_ordering'

export interface OrderingCapabilities {
  product_detail_enabled: boolean
  show_item_images: boolean
  show_item_descriptions: boolean
  item_notes_enabled: boolean
  request_bill_enabled: boolean
  customer_payment_enabled: boolean
  payment_link_enabled: boolean
  pay_at_counter_enabled: boolean
  add_to_running_table_enabled: boolean
  feedback_enabled: boolean
}

export type OrderingLayout = 'Mobile' | 'Tablet' | 'Landscape Kiosk' | 'Portrait Kiosk'

export interface OrderingContext {
  session: string
  source: string
  restaurant: string
  table: string | null
  layout: OrderingLayout
  capabilities: OrderingCapabilities
}

export interface MenuItem {
  item: string
  item_name: string
  rate: number
  special_dish: number
  disabled: number
  item_image: string | null
  course: string | null
  course_label: string | null
}

export interface MenuResponse {
  items: MenuItem[]
  modified_time: string | null
  name: string | null
}

export interface OrderItem {
  item_code: string
  item_name: string
  qty: number
  comment: string | null
  rate: number
  amount: number
}

export interface CustomerOrder {
  invoice: string | null
  table?: string | null
  order_type?: string
  // Pickup reference code (derived from the invoice name) — the only
  // identifier a QR Pickup customer has, since there's no table to point
  // to. Present (possibly null before an order exists) on every response.
  pickup_code?: string | null
  items: OrderItem[]
  grand_total: number
  billed: boolean
}

export interface OrderStatus {
  session_status: string
  invoice: string | null
  billed?: boolean
  submitted?: boolean
  open_requests?: { name: string; request_type: string; status: string }[]
}

export interface FeedbackParameter {
  parameter: string
  label: string
}

export interface FeedbackForm {
  status: 'open' | 'received' | 'not_eligible'
  parameters: FeedbackParameter[]
}

export interface FeedbackRating {
  parameter: string
  rating: number
}

// Persist the full ordering context (not just the opaque session token)
// across the browser session — a page refresh/resume must not force a
// customer to rescan, and must not lose capabilities/table/layout either
// (a session-token-only resume can't reconstruct those without another
// round trip the backend doesn't offer for an already-open session).
const SESSION_KEY = 'ury_order_session'
const CONTEXT_KEY = 'ury_order_context'

export function getStoredSession(): string | null {
  return sessionStorage.getItem(SESSION_KEY)
}

export function getStoredContext(): (OrderingContext & { qrToken?: string }) | null {
  const raw = sessionStorage.getItem(CONTEXT_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as OrderingContext & { qrToken?: string }
  } catch {
    return null
  }
}

function storeContext(context: OrderingContext, qrToken?: string) {
  sessionStorage.setItem(SESSION_KEY, context.session)
  sessionStorage.setItem(CONTEXT_KEY, JSON.stringify({ ...context, qrToken }))
}

// Every whitelisted Frappe method response is wrapped as {"message": <actual
// return value>} — call.get/call.post return that whole envelope as-is
// (frappe-js-sdk does not unwrap it; see pos/src/lib/menu-api.ts's
// GetMenuResponse for the same established pattern in this codebase). Every
// function below must unwrap `.message` itself. Missing this doesn't throw —
// it silently produces `undefined` fields (e.g. `context.session` would be
// undefined instead of a string), which cascades into every subsequent call
// silently sending no value for that parameter at all. Confirmed live via
// browser network capture: get_customer_menu was called with an entirely
// empty query string because the "session" it was given was undefined.
interface FrappeResponse<T> {
  message: T
}

export async function bootstrap(token: string): Promise<OrderingContext> {
  // A reload keeps the QR in the URL. Resume only the context from that
  // same QR; a different table/link must still bootstrap its own session.
  // Menu/order reads continue to validate the opaque session server-side.
  const stored = getStoredContext()
  if (stored?.session && stored.qrToken === token) return stored
  const response = await call.get<FrappeResponse<OrderingContext>>(`${M}.get_ordering_context`, { token })
  storeContext(response.message, token)
  return response.message
}

export async function bootstrapDevice(deviceId: string, deviceCredential: string): Promise<OrderingContext> {
  const response = await call.get<FrappeResponse<OrderingContext>>(`${M}.get_ordering_context`, {
    device_id: deviceId,
    device_credential: deviceCredential,
  })
  storeContext(response.message)
  return response.message
}

export async function assignDeviceTable(
  deviceId: string,
  deviceCredential: string,
  staffPin: string,
  table: string,
): Promise<OrderingContext> {
  const response = await call.post<FrappeResponse<OrderingContext>>(`${M}.assign_device_table`, {
    device_id: deviceId,
    device_credential: deviceCredential,
    staff_pin: staffPin,
    table,
  })
  storeContext(response.message)
  return response.message
}

export async function getMenu(session: string): Promise<MenuResponse> {
  const response = await call.get<FrappeResponse<MenuResponse>>(`${M}.get_customer_menu`, { session })
  return response.message
}

export async function getProductAddons(session: string, item_code: string): Promise<{ item_code: string }[]> {
  const response = await call.get<FrappeResponse<{ addons: { item_code: string }[] }>>(`${M}.get_customer_product`, { session, item_code })
  if (!Array.isArray(response.message?.addons)) throw new Error('Invalid add-on response')
  return response.message.addons
}

export async function getCurrentOrder(session: string): Promise<CustomerOrder> {
  const response = await call.get<FrappeResponse<CustomerOrder>>(`${M}.get_customer_order`, { session })
  return response.message
}

export async function addItems(
  session: string,
  items: { item: string; qty: number; comment?: string }[],
): Promise<CustomerOrder> {
  const response = await call.post<FrappeResponse<CustomerOrder>>(`${M}.add_customer_items`, {
    session,
    items: JSON.stringify(items),
  })
  return response.message
}

export async function requestBill(session: string): Promise<{ status: string; request: string }> {
  const response = await call.post<FrappeResponse<{ status: string; request: string }>>(`${M}.request_bill`, { session })
  return response.message
}

export async function requestAssistance(session: string): Promise<{ status: string; request: string }> {
  const response = await call.post<FrappeResponse<{ status: string; request: string }>>(`${M}.request_assistance`, { session })
  return response.message
}

export async function getStatus(session: string): Promise<OrderStatus> {
  const response = await call.get<FrappeResponse<OrderStatus>>(`${M}.get_order_status`, { session })
  return response.message
}

export async function getFeedbackForm(session?: string, receiptToken?: string): Promise<FeedbackForm> {
  const credentials = session ? { session } : receiptToken ? { receipt_token: receiptToken } : {}
  const response = await call.post<FrappeResponse<FeedbackForm>>(`${M}.get_feedback_form`, credentials)
  return response.message
}

export async function submitFeedback(
  ratings: FeedbackRating[],
  session?: string,
  receiptToken?: string,
): Promise<{ status: string }> {
  const credentials = session ? { session } : { receipt_token: receiptToken }
  const response = await call.post<FrappeResponse<{ status: string }>>(`${M}.submit_feedback`, {
    ...credentials,
    ratings: JSON.stringify(ratings),
  })
  return response.message
}

export interface PaymentRequestResult {
  payment_request: string
  amount: number
  currency: string
  payment_url: string | null
  status: string
  paid_amount?: number
  remaining_amount?: number
}

export interface PaymentStatusResult {
  status: string | null
  payment_request?: string
  amount?: number
  payment_url?: string | null
}

export async function createPaymentRequest(session: string): Promise<PaymentRequestResult> {
  const response = await call.post<FrappeResponse<PaymentRequestResult>>(`${M}.create_payment_request`, { session })
  return response.message
}

export async function getPaymentStatus(session: string): Promise<PaymentStatusResult> {
  const response = await call.get<FrappeResponse<PaymentStatusResult>>(`${M}.get_payment_status`, { session })
  return response.message
}

export async function sharePaymentLink(
  session: string,
  recipient: string,
): Promise<{ status: string; payment_request: string }> {
  const response = await call.post<FrappeResponse<{ status: string; payment_request: string }>>(
    `${M}.share_payment_link`,
    { session, recipient },
  )
  return response.message
}
