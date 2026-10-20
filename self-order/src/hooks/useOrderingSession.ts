import { useCallback, useEffect, useRef, useState } from 'react'
import { parseFrappeError } from '@ury/core'
import {
  addItems,
  bootstrap,
  bootstrapGrantedSession,
  createPaymentRequest,
  getCurrentOrder,
  getMenu,
  getProductAddons,
  getStoredContext,
  requestBill,
  requestAssistance,
  type CustomerOrder,
  type MenuItem,
  type OrderingContext,
  type PaymentRequestResult,
} from '../lib/api'
import { paymentOutcome } from '../lib/payment-outcome'
import { t } from '../i18n'
import { useGuestLanguage } from '../components/LanguageSwitcher'

const CATALOG_FALLBACK = '\u0000'

type CatalogErrorKey =
  | 'missing_code'
  | 'load_menu'
  | 'place_order'
  | 'request_bill'
  | 'call_waiter'
  | 'pay_online'

type ShownError =
  | { source: 'catalog'; key: CatalogErrorKey }
  | { source: 'server'; text: string }

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

function serverTextOrCatalog(err: unknown, key: CatalogErrorKey, stripCodes = false): ShownError {
  const parsed = parseFrappeError(err, CATALOG_FALLBACK)
  if (parsed === CATALOG_FALLBACK) return { source: 'catalog', key }
  // Older ERP responses contain both the refusal and a wrapped copy.
  // Remove that legacy wrapper, not the translated reason or other errors.
  const text = stripCodes ? [...new Set(parsed
    .replace(/\[[A-Z][A-Z0-9_-]*\]\s*/g, '')
    .split('\n')
    .map(line => line.replace(/^Error while placing order:\s*/, '').trim())
    .filter(Boolean))].join('\n') : parsed
  return { source: 'server', text }
}

// Same keys api.ts uses internally for sessionStorage persistence. api.ts
// doesn't expose a clear function (only get/store), so resetSession clears
// them directly here rather than changing api.ts's exported surface.
const SESSION_KEY = 'ury_order_session'
const CONTEXT_KEY = 'ury_order_context'

export type GuestLine = { key: string; item: MenuItem; qty: number; note: string }
export type AddonState = { status: 'loading' | 'error' | 'ready'; items: MenuItem[] }
type Cart = Record<string, GuestLine>

export function hasPrice(item: MenuItem): boolean {
  return typeof item.rate === 'number' && Number.isFinite(item.rate) && item.rate >= 0
}

// A raw invoice comment has no guest provenance. Never promote it into
// guest UI state, even when notes are enabled. Network projection is U7.
function guestOrder(current: CustomerOrder): CustomerOrder {
  return {
    invoice: current.invoice, table: current.table, order_type: current.order_type,
    pickup_code: current.pickup_code, grand_total: current.grand_total, billed: current.billed,
    items: current.items.map(row => ({
      item_code: row.item_code, item_name: row.item_name, qty: row.qty,
      rate: row.rate, amount: row.amount, comment: null,
    })),
  }
}

function addLine(cart: Cart, item: MenuItem, note: string): Cart {
  const key = JSON.stringify([item.item, note])
  return { ...cart, [key]: { key, item, note, qty: (cart[key]?.qty ?? 0) + 1 } }
}

function useQueryToken(): string | null {
  const params = new URLSearchParams(window.location.search)
  return params.get('t')
}

function useFragmentGrant(): string | null {
  const match = window.location.hash.match(/^#g=(.+)$/)
  return match ? decodeURIComponent(match[1]) : null
}

function consumeFragmentGrant() {
  const url = new URL(window.location.href)
  url.hash = ''
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}`)
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
  const grantToken = useRef(useFragmentGrant()).current
  const [context, setContext] = useState<OrderingContext | null>(null)
  const [menu, setMenu] = useState<MenuItem[]>([])
  const [order, setOrder] = useState<CustomerOrder | null>(null)
  const [cart, setCart] = useState<Cart>({})
  const [addonsByItem, setAddonsByItem] = useState<Record<string, AddonState>>({})
  const [noteItem, setNoteItem] = useState<MenuItem | null>(null)
  const [noteText, setNoteText] = useState('')
  const addonGeneration = useRef(0)
  const addonPending = useRef(0)
  const addonTail = useRef<Promise<void>>(Promise.resolve())
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [shownError, setShownError] = useState<ShownError | null>(null)
  const [billRequested, setBillRequested] = useState(false)
  const [requestingAssistance, setRequestingAssistance] = useState(false)
  const [assistanceCooldown, setAssistanceCooldown] = useState(false)
  const [assistanceStatus, setAssistanceStatus] = useState<string | null>(null)
  useGuestLanguage()
  const assistanceInFlight = useRef(false)
  const [paymentRequest, setPaymentRequest] = useState<PaymentRequestResult | null>(null)
  const [paymentNotice, setPaymentNotice] = useState<string | null>(null)
  const [payingOnline, setPayingOnline] = useState(false)

  const loadOrder = useCallback(async (session: string) => {
    const current = await getCurrentOrder(session)
    setOrder(guestOrder(current))
  }, [])

  const loadAddons = useCallback(async (session: string, item: MenuItem, items: MenuItem[], generation: number) => {
    const update = (state: AddonState) => {
      if (addonGeneration.current === generation) setAddonsByItem(prev => ({ ...prev, [item.item]: state }))
    }
    update({ status: 'loading', items: [] })
    try {
      const linked = await getProductAddons(session, item.item)
      // Availability and displayed price come from the current server menu,
      // not an invented zero or a stale/disabled/off-menu linked product.
      const codes = new Set(linked.map(addon => addon.item_code))
      update({ status: 'ready', items: items.filter(candidate => !candidate.disabled && codes.has(candidate.item)) })
    } catch {
      update({ status: 'error', items: [] })
    }
  }, [])

  function enqueueAddonRead(generation: number, read: () => Promise<void>) {
    const run = () => (addonGeneration.current === generation ? read() : undefined)
    addonPending.current += 1
    const started = addonPending.current === 1
      ? new Promise<void>(resolve => { resolve(run()) })
      : addonTail.current.then(run)
    addonTail.current = started.then(() => undefined, () => undefined).then(() => {
      addonPending.current -= 1
    })
  }

  const init = useCallback(async () => {
    const generation = ++addonGeneration.current
    setLoading(true)
    try {
      let ctx: OrderingContext
      if (initialContext) {
        ctx = initialContext
      } else if (grantToken) {
        consumeFragmentGrant()
        ctx = await bootstrapGrantedSession(grantToken)
      } else if (token) {
        ctx = await bootstrap(token)
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
      const menuResponse = await getMenu(ctx.session)
      const items = menuResponse.items.filter((item) => !item.disabled)
      setMenu(items)
      await loadOrder(ctx.session)
      setContext(ctx)
      // One optional read at a time, including a visible retry, and only after
      // the order is on screen. A hung add-on must not fill the HTTP/1.1 pool.
      if (ctx.capabilities.product_detail_enabled) {
        void (async () => {
          for (const item of items) {
            if (addonGeneration.current !== generation) return
            enqueueAddonRead(generation, () => loadAddons(ctx.session, item, items, generation))
            await addonTail.current
          }
        })()
      }
    } catch (err) {
      setShownError(serverTextOrCatalog(err, 'load_menu'))
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grantToken, initialContext, token])

  useEffect(() => {
    init()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
    setAddonsByItem({})
    setNoteItem(null)
    setNoteText('')
    setOrder(null)
    setBillRequested(false)
    setAssistanceStatus(null)
    setPaymentRequest(null)
    setPaymentNotice(null)
    setPayingOnline(false)
    setSubmitting(false)
    setShownError(null)
    setContext(null)
    setMenu([])
    init()
  }, [init])

  function addToCart(item: MenuItem) {
    if (item.disabled || !hasPrice(item)) return
    if (context?.capabilities.item_notes_enabled) {
      setNoteItem(item)
      setNoteText('')
    } else setCart(prev => addLine(prev, item, ''))
  }

  function cancelNote() { setNoteItem(null); setNoteText('') }

  function confirmNote() {
    if (!noteItem || !hasPrice(noteItem)) return
    setCart(prev => addLine(prev, noteItem, context?.capabilities.item_notes_enabled ? noteText.trim().slice(0, 140) : ''))
    cancelNote()
  }

  function incrementLine(line: GuestLine) {
    if (hasPrice(line.item)) setCart(prev => addLine(prev, line.item, line.note))
  }

  function retryAddons(itemCode: string) {
    const item = menu.find(candidate => candidate.item === itemCode)
    if (!context || !item) return
    const generation = addonGeneration.current
    setAddonsByItem(prev => ({ ...prev, [item.item]: { status: 'loading', items: [] } }))
    enqueueAddonRead(generation, () => loadAddons(context.session, item, menu, generation))
  }

  function decrementCart(itemCode: string) {
    setCart((prev) => {
      const existing = prev[itemCode]
      if (!existing) return prev
      if (existing.qty <= 1) {
        const rest = { ...prev }
        delete rest[itemCode]
        return rest
      }
      return { ...prev, [itemCode]: { ...existing, qty: existing.qty - 1 } }
    })
  }

  const cartItems = Object.values(cart)
  const cartByItem: Record<string, { item: MenuItem; qty: number }> = {}
  for (const line of cartItems) {
    cartByItem[line.item.item] = { item: line.item, qty: (cartByItem[line.item.item]?.qty ?? 0) + line.qty }
  }
  const cartCount = cartItems.reduce((sum, entry) => sum + entry.qty, 0)
  const cartTotal = cartItems.reduce((sum, entry) => sum + entry.qty * entry.item.rate, 0)

  async function submitCart() {
    if (!context || cartItems.length === 0 || cartItems.some(line => !hasPrice(line.item))) return
    setSubmitting(true)
    setShownError(null)
    try {
      const payload = cartItems.map(entry => ({
        item: entry.item.item, qty: entry.qty,
        ...(context.capabilities.item_notes_enabled && entry.note ? { comment: entry.note } : {}),
      }))
      const updated = await addItems(context.session, payload)
      setOrder(guestOrder(updated))
      setCart({})
    } catch (err) {
      setShownError(serverTextOrCatalog(err, 'place_order', true))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleRequestBill() {
    if (!context) return
    try {
      await requestBill(context.session)
      setBillRequested(true)
    } catch (err) {
      setShownError(err instanceof Error ? { source: 'server', text: err.message } : { source: 'catalog', key: 'request_bill' })
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
      setShownError(serverTextOrCatalog(err, 'call_waiter'))
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
      const outcome = paymentOutcome(result)
      setPaymentNotice(outcome.notice ? t(outcome.notice.key, outcome.notice.params) : null)
      if (outcome.redirect) {
        window.location.href = outcome.redirect
      }
    } catch (err) {
      // Includes the graceful "online payment isn't set up yet" case from
      // the backend — surfaced as a normal error message, not a crash.
      setShownError(err instanceof Error ? { source: 'server', text: err.message } : { source: 'catalog', key: 'pay_online' })
    } finally {
      setPayingOnline(false)
    }
  }

  const error = shownError == null
    ? null
    : shownError.source === 'server'
      ? shownError.text
      : catalogError(shownError.key)
  const assistanceMessage = assistanceStatus == null
    ? null
    : assistanceStatus === 'Already Requested'
      ? t('assistance.already')
      : t('assistance.requested')

  return {
    context,
    menu,
    order,
    cart: cartByItem,
    addonsByItem,
    retryAddons,
    noteItem,
    noteText,
    changeNote: (text: string) => setNoteText(text.slice(0, 140)),
    confirmNote,
    cancelNote,
    incrementLine,
    loading,
    submitting,
    error,
    billRequested,
    requestingAssistance,
    assistanceCooldown,
    assistanceMessage,
    handleRequestAssistance,
    paymentRequest,
    paymentNotice,
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
  }
}
