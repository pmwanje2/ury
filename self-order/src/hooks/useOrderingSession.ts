import { useCallback, useEffect, useRef, useState } from 'react'
import { parseFrappeError, clientMessage } from '@ury/core'
import {
  addItems,
  bootstrap,
  createPaymentRequest,
  getCurrentOrder,
  getMenu,
  getStatus,
  getStoredContext,
  requestAssistance,
  requestBill,
  type CustomerOrder,
  type MenuItem,
  type OrderingContext,
  type OrderStatus,
  type PaymentRequestResult,
} from '../lib/api'
import { paymentOutcome } from '../lib/payment-outcome'
import { getAvailabilityMessage, getMenuAvailability, isUnavailable, type MenuAvailability } from '../lib/availability'
import { t } from '../i18n'
import { useGuestLanguage } from '../components/LanguageSwitcher'

// Sentinel for "no ERP sentence": the error is then a catalog key, translated
// each render so a language switch retranslates an error already on screen.
const CATALOG_FALLBACK = '\u0000'

type CatalogErrorKey = 'missing_code' | 'load_menu' | 'place_order' | 'request_bill' | 'call_waiter' | 'pay_online'

type ShownError =
  | { source: 'catalog'; key: CatalogErrorKey }
  | { source: 'server'; text: string }
  | { source: 'availability'; itemName: string; reason: string }

function catalogError(key: CatalogErrorKey): string {
  switch (key) {
    case 'missing_code':
      return t('errors.missing_code')
    case 'load_menu':
      return t('errors.load_menu')
    case 'place_order':
      return t('errors.place_order')
    case 'request_bill':
      return t('errors.request_bill')
    case 'call_waiter':
      return t('errors.call_waiter')
    case 'pay_online':
      return t('errors.pay_online')
  }
}

// The ERP sentence is already in the guest's language (Frappe translates it
// with the preferred_language cookie), so it is shown as sent. stripCodes
// removes refusal ids and the legacy "Error while placing order:" copy.
function serverTextOrCatalog(err: unknown, key: CatalogErrorKey, stripCodes = false): ShownError {
  const parsed = parseFrappeError(err, CATALOG_FALLBACK)
  if (parsed === CATALOG_FALLBACK) return { source: 'catalog', key }
  const text = stripCodes ? [...new Set(parsed
    .replace(/\[[A-Z][A-Z0-9_-]*\]\s*/g, '')
    .split('\n')
    .map(line => line.replace(/^Error while placing order:\s*/, '').trim())
    .filter(Boolean))].join('\n') : parsed
  return { source: 'server', text: clientMessage(text) }
}

// Same keys api.ts uses internally for sessionStorage persistence. api.ts
// doesn't expose a clear function (only get/store), so resetSession clears
// them directly here rather than changing api.ts's exported surface.
const SESSION_KEY = 'ury_order_session'
const CONTEXT_KEY = 'ury_order_context'

// One cart line per (item, kitchen note): the same dish with different notes
// stays on separate lines, equal notes merge. `comment` is the only per-line
// field `add_customer_items` accepts; `variant` is display-only. Selected
// add-ons become their own priced lines (see addToCart), never a hidden extra.
export interface CartEntry {
  key?: string
  item: MenuItem
  qty: number
  comment?: string
  variant?: string
}

type Cart = Record<string, CartEntry>

/** Native comment fields (POS Invoice Item.comment / URY KOT Items.comments) are Data. */
export const MAX_NOTE_LENGTH = 140

/** A missing or non-finite rate is unknown, never free; a real zero is allowed. */
export function hasPrice(item: MenuItem): boolean {
  return typeof item.rate === 'number' && Number.isFinite(item.rate) && item.rate >= 0
}

function lineKey(itemCode: string, note: string): string {
  return note ? JSON.stringify([itemCode, note]) : itemCode
}

function addLine(cart: Cart, item: MenuItem, note: string, variant?: string): Cart {
  const key = lineKey(item.item, note)
  const existing = cart[key]
  return {
    ...cart,
    [key]: {
      key,
      item,
      qty: (existing?.qty ?? 0) + 1,
      comment: note || undefined,
      variant: variant ?? existing?.variant,
    },
  }
}

// Screen state machine shared by all 4 layout shells (Mobile/Tablet/
// Landscape Kiosk/Portrait Kiosk) instead of a router — deliberate per the
// plan: react-router-dom is present in package.json but unused, and a
// single hook composed by every layout is simpler than a router shared
// across layout shells that each render a completely different chrome.
export type Screen = 'menu' | 'detail' | 'cart' | 'checkout' | 'status'

// The server refused the session itself (expired, closed or unknown): an
// authorization denial, never a validation or transport failure.
function isSessionRefused(err: unknown): boolean {
  const e = err as { httpStatus?: number; exc_type?: string } | null
  return e?.httpStatus === 403 || (typeof e?.exc_type === 'string' && e.exc_type.endsWith('PermissionError'))
}

function useQueryToken(): string | null {
  const params = new URLSearchParams(window.location.search)
  return params.get('t')
}

/**
 * Owns all the stateful ordering logic shared across layouts: token
 * bootstrap, menu/order loading, cart management, order submission, and
 * bill requests. Layout components consume this hook and are responsible
 * only for rendering.
 *
 * Pass `initialContext` when the caller has already resolved a context
 * through some other bootstrap path (e.g. device-credential bootstrap for
 * a kiosk/tablet, done once in App.tsx before a layout is chosen) — this
 * skips the QR-token/session-resume bootstrap entirely and loads menu/order
 * directly against the given context, so a device only ever bootstraps once.
 */
export function useOrderingSession(initialContext?: OrderingContext) {
  const token = useQueryToken()
  const [context, setContext] = useState<OrderingContext | null>(null)
  const [menu, setMenu] = useState<MenuItem[]>([])
  const [availability, setAvailability] = useState<MenuAvailability>({})
  const availabilityRequest = useRef(0)
  const [order, setOrder] = useState<CustomerOrder | null>(null)
  const [orderStatus, setOrderStatus] = useState<OrderStatus | null>(null)
  const [cart, setCart] = useState<Cart>({})
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const submissionInFlight = useRef(false)
  const [shownError, setShownError] = useState<ShownError | null>(null)
  const [billRequested, setBillRequested] = useState(false)
  const [requestingAssistance, setRequestingAssistance] = useState(false)
  const [assistanceCooldown, setAssistanceCooldown] = useState(false)
  const [assistanceStatus, setAssistanceStatus] = useState<string | null>(null)
  const assistanceInFlight = useRef(false)
  useGuestLanguage()
  const [paymentRequest, setPaymentRequest] = useState<PaymentRequestResult | null>(null)
  const [payingOnline, setPayingOnline] = useState(false)
  const [screen, setScreen] = useState<Screen>('menu')
  // The item a `detail` screen should render for — set by goToDetail,
  // cleared whenever navigation leaves `detail` so a stale item never
  // lingers into the next detail visit.
  const [detailItemCode, setDetailItemCode] = useState<string | null>(null)

  const loadOrder = useCallback(async (session: string) => {
    const current = await getCurrentOrder(session)
    setOrder(current)
  }, [])

  const init = useCallback(async () => {
    setLoading(true)
    try {
      let ctx: OrderingContext
      let resumed = false
      if (initialContext) {
        ctx = initialContext
      } else if (token) {
        const storedSession = getStoredContext()?.session
        ctx = await bootstrap(token)
        resumed = !!storedSession && storedSession === ctx.session
      } else {
        const storedContext = getStoredContext()
        if (storedContext) {
          // No fresh token in the URL (e.g. a bookmarked/refreshed page)
          // — reuse the full context saved at bootstrap time, not just
          // the session token, so capabilities/table/layout survive a
          // refresh too. The backend still rejects the session once it
          // actually expires.
          ctx = storedContext
        } else {
          setShownError({ source: 'catalog', key: 'missing_code' })
          setLoading(false)
          return
        }
      }
      const load = async (session: string) => {
        const menuResponse = await getMenu(session)
        setMenu(menuResponse.items.filter((item) => !item.disabled))
        setAvailability(menuResponse.availability ?? {})
        await loadOrder(session)
      }
      try {
        await load(ctx.session)
      } catch (err) {
        // A resumed session the server no longer accepts: start once from the
        // QR still in the URL, so a reload or rescan in the same tab recovers.
        if (!resumed || !token || !isSessionRefused(err)) throw err
        ctx = await bootstrap(token, { resume: false })
        await load(ctx.session)
      }
      // Only a session the server still accepts shows its table and order.
      setContext(ctx)
    } catch (err) {
      setShownError(serverTextOrCatalog(err, 'load_menu'))
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialContext, token])

  useEffect(() => {
    init()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const session = context?.session
  useEffect(() => {
    if (!session) return
    const refresh = () => {
      if (document.visibilityState !== 'visible') return
      const request = ++availabilityRequest.current
      void getMenuAvailability(session).then((next) => {
        if (request === availabilityRequest.current) setAvailability(next)
      }).catch(() => {
        // Display-only refresh: don't replace the last successful snapshot.
      })
    }
    const timer = setInterval(refresh, 60_000)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      ++availabilityRequest.current
      clearInterval(timer)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [session])

  useEffect(() => {
    if (!assistanceCooldown) return
    const timer = setTimeout(() => setAssistanceCooldown(false), 60000)
    return () => clearTimeout(timer)
  }, [assistanceCooldown])

  /**
   * Manual "start a fresh order" action — the MVP alternative to an
   * auto-idle-reset timer (not wired up yet). Always clears the cart and
   * any in-memory order/payment/bill state, and always clears both
   * sessionStorage keys so a stale session/context can never leak into the
   * next customer.
   *
   * For device-bootstrapped sessions (kiosk/tablet — `initialContext` was
   * passed to the hook) there's a durable device credential behind the
   * context, so we can immediately re-bootstrap a fresh session via `init`
   * without sending the customer back through a QR scan.
   *
   * For QR/link-based sessions there is no device credential to re-derive
   * a session from — clearing storage here means the next `init` run finds
   * neither a token override nor a stored context, so the hook falls back
   * to its normal "missing ordering code" error state and the customer (or
   * staff) must rescan/re-open the link. That is the correct outcome, not
   * a bug: a QR session's only source of truth is the token in the URL.
   */
  const resetSession = useCallback(() => {
    sessionStorage.removeItem(SESSION_KEY)
    sessionStorage.removeItem(CONTEXT_KEY)
    setCart({})
    setOrder(null)
    setOrderStatus(null)
    setBillRequested(false)
    setAssistanceStatus(null)
    setPaymentRequest(null)
    setPayingOnline(false)
    setSubmitting(false)
    setShownError(null)
    setContext(null)
    setMenu([])
    ++availabilityRequest.current
    setAvailability({})
    setScreen('menu')
    setDetailItemCode(null)
    init()
  }, [init])

  function addToCart(
    item: MenuItem,
    options?: { comment?: string; variant?: string; addons?: string[] },
  ) {
    if (item.disabled || !hasPrice(item) || isUnavailable(availability[item.item])) return
    const note = context?.capabilities.item_notes_enabled
      ? (options?.comment ?? '').trim().slice(0, MAX_NOTE_LENGTH)
      : ''
    // Add-ons are billed as their own menu lines at the server menu price;
    // off-menu, disabled or unpriced add-ons are not orderable.
    const addons = (options?.addons ?? [])
      .map((code) => menu.find((candidate) => candidate.item === code))
      .filter((addon): addon is MenuItem =>
        !!addon && !addon.disabled && hasPrice(addon) && !isUnavailable(availability[addon.item]))
    setCart((prev) => {
      let next = addLine(prev, item, note, options?.variant)
      for (const addon of addons) next = addLine(next, addon, '')
      return next
    })
  }

  // --- screen navigation -----------------------------------------------
  function goToMenu() {
    setDetailItemCode(null)
    setScreen('menu')
  }

  function goToDetail(itemCode: string) {
    setDetailItemCode(itemCode)
    setScreen('detail')
  }

  function goToCart() {
    setScreen('cart')
  }

  function goToCheckout() {
    setScreen('checkout')
  }

  // Order status (session_status/invoice/billed/submitted/open_requests) is
  // fetched from the real `get_order_status` endpoint rather than derived
  // from `CustomerOrder` — `open_requests` (service requests) has no
  // equivalent on `CustomerOrder` and can't be reconstructed client-side.
  const loadStatus = useCallback(async () => {
    if (!context) return
    try {
      const latest = await getStatus(context.session)
      setOrderStatus(latest)
    } catch (err) {
      setShownError(serverTextOrCatalog(err, 'load_menu'))
    }
  }, [context])

  function goToStatus() {
    setScreen('status')
    loadStatus()
  }

  /** `lineKey` is a cart key: the item code for a plain line, or the noted line's key. */
  function decrementCart(lineKey: string) {
    setCart((prev) => {
      const existing = prev[lineKey]
      if (!existing) return prev
      if (existing.qty <= 1) {
        const rest = { ...prev }
        delete rest[lineKey]
        return rest
      }
      return { ...prev, [lineKey]: { ...existing, qty: existing.qty - 1 } }
    })
  }

  const cartItems = Object.values(cart)
  const cartCount = cartItems.reduce((sum, entry) => sum + entry.qty, 0)
  const cartTotal = cartItems.reduce((sum, entry) => sum + entry.qty * entry.item.rate, 0)

  // Returns whether the order actually reached the backend, so callers can
  // gate post-submit navigation (e.g. to the status screen) on real success
  // instead of on the promise merely settling — this function intentionally
  // never rethrows (errors are surfaced via `error` state for the checkout
  // screen to render inline), so a caller doing `submitCart().then(...)`
  // without checking this return value would navigate to "Order confirmed"
  // even when submission failed.
  async function submitCart(tip?: string): Promise<boolean> {
    if (!context || cartItems.length === 0 || submissionInFlight.current) return false
    submissionInFlight.current = true
    setSubmitting(true)
    setShownError(null)
    try {
      const request = ++availabilityRequest.current
      const fresh = await getMenuAvailability(context.session)
      if (request === availabilityRequest.current) setAvailability(fresh)
      for (const entry of cartItems) {
        const itemAvailability = fresh[entry.item.item]
        if (!itemAvailability || isUnavailable(itemAvailability)) {
          setShownError({
            source: 'availability', itemName: entry.item.item_name,
            reason: itemAvailability?.reason_code ?? 'MENU_ITEM_DISABLED',
          })
          return false
        }
      }

      const notesEnabled = context.capabilities.item_notes_enabled
      const payload = cartItems.map((entry) => ({
        item: entry.item.item,
        qty: entry.qty,
        ...(notesEnabled && entry.comment ? { comment: entry.comment } : {}),
      }))
      const updated = await addItems(context.session, payload, ...(tip === undefined ? [] : [tip]))
      setOrder(updated)
      setCart({})
      return true
    } catch {
      setShownError({ source: 'catalog', key: 'place_order' })
      return false
    } finally {
      submissionInFlight.current = false
      setSubmitting(false)
    }
  }

  async function handleRequestBill() {
    if (!context) return
    try {
      await requestBill(context.session)
      setBillRequested(true)
    } catch (err) {
      setShownError(serverTextOrCatalog(err, 'request_bill', true))
    }
  }

  async function handleRequestAssistance() {
    if (!context || assistanceInFlight.current || assistanceCooldown) return
    assistanceInFlight.current = true
    setRequestingAssistance(true)
    setShownError(null)
    setAssistanceStatus(null)
    try {
      const result = await requestAssistance(context.session)
      setAssistanceStatus(result.status)
      setAssistanceCooldown(true)
    } catch (err) {
      setShownError(serverTextOrCatalog(err, 'call_waiter', true))
    } finally {
      assistanceInFlight.current = false
      setRequestingAssistance(false)
    }
  }

  async function payOnline() {
    if (!context) return
    setPayingOnline(true)
    setShownError(null)
    try {
      const result = await createPaymentRequest(context.session)
      setPaymentRequest(result)
      const { redirect } = paymentOutcome(result)
      if (redirect) {
        window.location.href = redirect
      }
    } catch (err) {
      // Includes the graceful "online payment isn't set up yet" case from
      // the backend — surfaced as a normal error message, not a crash.
      setShownError(serverTextOrCatalog(err, 'pay_online'))
    } finally {
      setPayingOnline(false)
    }
  }

  const error = shownError == null
    ? null
    : shownError.source === 'server'
      ? shownError.text
      : shownError.source === 'availability'
        ? `${shownError.itemName}: ${getAvailabilityMessage(shownError.reason)}`
        : catalogError(shownError.key)
  const assistanceMessage = assistanceStatus == null
    ? null
    : assistanceStatus === 'Already Requested'
      ? t('assistance.already')
      : t('assistance.requested')

  return {
    context,
    menu,
    availability,
    order,
    orderStatus,
    cart,
    loading,
    submitting,
    error,
    billRequested,
    requestingAssistance,
    assistanceCooldown,
    assistanceMessage,
    handleRequestAssistance,
    paymentRequest,
    payingOnline,
    addToCart,
    decrementCart,
    submitCart,
    handleRequestBill,
    payOnline,
    resetSession,
    cartItems,
    cartCount,
    cartTotal,
    // Screen state machine
    screen,
    setScreen,
    detailItemCode,
    goToMenu,
    goToDetail,
    goToCart,
    goToCheckout,
    goToStatus,
  }
}
