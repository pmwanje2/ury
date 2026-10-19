import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import { initI18n } from './i18n'
import { useOrderingSession } from './hooks/useOrderingSession'
import Mobile from './layouts/MobileQRLayout'
import Tablet from './layouts/TabletLayout'
import Landscape from './layouts/LandscapeKioskLayout'
import Portrait from './layouts/PortraitKioskLayout'
import type { CustomerOrder, MenuItem, OrderingContext } from './lib/api'

// Real layouts, hook, i18n and API serialization. Only the HTTP boundary is doubled.
const context: OrderingContext = {
  session: 'test-session', restaurant: 'Salama test', table: 'T-12', source: 'QR Table', layout: 'Mobile',
  capabilities: {
    product_detail_enabled: true, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: true, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true,
    feedback_enabled: false,
  },
}
const item = (code: string, rate: number | null = 6000, disabled = 0) => ({
  item: code, item_name: code, rate, disabled, special_dish: 0, item_image: null, course: null, course_label: null,
})
const privateText = 'PRIVATE STAFF: allergy and internal instruction'
const emptyOrder: CustomerOrder = { invoice: null, items: [], grand_total: 0, billed: false }
const privateOrder: CustomerOrder = {
  invoice: 'TEST-1', billed: false, grand_total: 12000,
  items: [{ item_code: 'Fish', item_name: 'Fish', qty: 1, rate: 12000, amount: 12000, comment: privateText }],
}
const labels = {
  en: { note: 'Note for the kitchen', add: 'Add Fish', addon: 'Add add-on Sauce', place: 'Place Order', retry: 'Retry add-ons', error: 'Could not load add-ons.', unavailable: 'Price unavailable', close: 'Close' },
  sw: { note: 'Ujumbe kwa jikoni', add: 'Ongeza Fish', addon: 'Ongeza nyongeza Sauce', place: 'Weka oda', retry: 'Jaribu nyongeza tena', error: 'Imeshindwa kupakia nyongeza.', unavailable: 'Bei haipatikani', close: 'Funga' },
  lg: { note: "Ekiwandiiko eri mu kyumba ky'emmere", add: 'Teekako Fish', addon: 'Yongerako ekyongerwako Sauce', place: 'Teeka oda', retry: 'Gezaako ebyongerwako nate', error: 'Ebyongerwako tebibadde bisobola kutikkibwa.', unavailable: 'Omuwendo tegulabika', close: 'Ggalawo' },
}
let root: Root
let host: HTMLDivElement
let menu: ReturnType<typeof item>[]
let addons: { item_code: string; item_name: string; rate: number | null }[]
let productFails: boolean
let serverOrder: CustomerOrder
let sent: { item: string; qty: number; comment?: string }[][]
let hook: ReturnType<typeof useOrderingSession>
let holdItem: string | null
let heldProductReads: number
let held: { resolve: () => void; reject: (reason?: unknown) => void }[]

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order')
  menu = [item('Fish', 12000), item('Sauce', 2000), item('Closed', 1000, 1), item('Free', 0)]
  addons = [{ item_code: 'Sauce', item_name: 'Sauce', rate: null }]
  productFails = false
  holdItem = null
  heldProductReads = 0
  held = []
  serverOrder = emptyOrder
  sent = []
  // Bill-bearing guest reads travel as POST bodies; only the static bootstrap may use GET.
  const read = async (path: string, params?: Record<string, unknown>) => {
    if (path.endsWith('get_customer_menu')) return { message: { items: menu, name: 'Test menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: serverOrder }
    if (path.endsWith('get_customer_product')) {
      if (productFails) throw new Error('503 synthetic add-on outage')
      const itemCode = String(params?.item_code ?? '')
      if (holdItem && itemCode === holdItem && heldProductReads === 0) {
        heldProductReads += 1
        const snapshot = addons.map(row => ({ ...row }))
        return await new Promise((resolve, reject) => {
          held.push({
            resolve: () => resolve({ message: { item_code: itemCode, variants: [], addons: snapshot } }),
            reject,
          })
        })
      }
      return { message: { item_code: params?.item_code, variants: [], addons: params?.item_code === 'Fish' ? addons : [] } }
    }
    throw new Error(`Unexpected read ${path}`)
  }
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw new Error(`Unexpected GET ${path}`) })
  vi.spyOn(call, 'post').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
    if (/\.get_customer_[a-z_]+$/.test(path)) return read(path, params)
    if (!path.endsWith('add_customer_items')) throw new Error(`Unexpected POST ${path}`)
    sent.push(JSON.parse(String(params?.items)))
    serverOrder = privateOrder
    return { message: serverOrder }
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  await initI18n('en', { setCookie() {} })
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  await initI18n('en', { setCookie() {} })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
async function render(node: React.ReactNode) {
  await act(async () => { root.render(node) })
}
function button(text: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll('button')].find(b => b.textContent?.trim() === text || b.getAttribute('aria-label') === text)
  expect(found, `executed behavior: button ${text}`).toBeDefined()
  return found!
}
async function click(target: HTMLElement) { await act(async () => target.click()) }
async function fish() {
  const found = [...host.querySelectorAll('button')].find(b => b.textContent?.trim().startsWith('Fish'))
  expect(found, 'real menu rendered').toBeDefined()
  await click(found!)
}
function dish(name: string): HTMLButtonElement | undefined {
  return [...host.querySelectorAll('button')].find(b =>
    b.textContent?.trim().startsWith(name) && !b.getAttribute('aria-label')?.startsWith('Add add-on'),
  ) as HTMLButtonElement | undefined
}
async function note(text: string) {
  const el = host.querySelector('textarea')
  expect(el, 'executed behavior: guest note input exists').not.toBeNull()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(el, text)
    el!.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function expand() {
  const toggle = host.querySelector<HTMLButtonElement>('button[aria-expanded="false"]')
  if (toggle) await click(toggle)
}
async function reload(node: React.ReactNode) {
  await act(async () => root.unmount())
  root = createRoot(host)
  await render(node)
}

for (const [layoutName, Layout] of Object.entries({ Mobile, Tablet, Landscape, Portrait })) {
  for (const lang of ['en', 'sw', 'lg'] as const) {
    const l = labels[lang]
    describe(`${layoutName}/${lang} real guest behavior`, () => {
      beforeEach(async () => { await initI18n(lang, { setCookie() {} }) })
      it('keeps differently noted lines separate, merges equal notes and adjusts the correct line; serializes no price', async () => {
        await render(<Layout initialContext={context} />)
        for (const text of ['no onions', 'extra chili', 'no onions']) {
          await fish()
          expect(host.querySelector('textarea'), 'guest must be able to enter a per-item note').not.toBeNull()
          expect(host.querySelector('label')?.textContent).toContain(l.note)
          await note(text)
          await click(button(l.add))
        }
        await expand()
        const rows = [...host.querySelectorAll('li')].filter(li => li.textContent?.includes('no onions') || li.textContent?.includes('extra chili'))
        expect(rows, 'two distinct client-note cart rows').toHaveLength(2)
        expect(rows[0].textContent).toContain('2')
        await click(rows[0].querySelectorAll('button')[0])
        await click(rows[1].querySelectorAll('button')[1])
        await click(button(l.place))
        expect(sent).toEqual([[{ item: 'Fish', qty: 1, comment: 'no onions' }, { item: 'Fish', qty: 2, comment: 'extra chili' }]])
        expect(host.textContent).not.toContain(privateText)
      })
      it('caps notes at 140, renders HTML as text, and cancels without adding', async () => {
        await render(<Layout initialContext={context} />)
        await fish()
        await note('x'.repeat(141))
        expect(host.querySelector('textarea')?.maxLength).toBe(140)
        expect(host.querySelector('textarea')?.value).toHaveLength(140)
        await click(button(l.add))
        await fish()
        await note('<img src=x onerror=alert(1)>')
        await click(button(l.add))
        await expand()
        expect(host.textContent).toContain('<img src=x onerror=alert(1)>')
        expect(host.querySelector('img')).toBeNull()
        await fish()
        await note('discard this')
        await click(button(l.close))
        expect(host.querySelector('textarea')).toBeNull()
        await click(button(l.place))
        expect(sent[0]).toEqual([{ item: 'Fish', qty: 1, comment: 'x'.repeat(140) }, { item: 'Fish', qty: 1, comment: '<img src=x onerror=alert(1)>' }])
      })
      it('does not expose or copy server-private comments with notes enabled or disabled, including reload/submit', async () => {
        serverOrder = privateOrder
        for (const enabled of [true, false]) {
          const ctx = { ...context, capabilities: { ...context.capabilities, item_notes_enabled: enabled } }
          await reload(<Layout initialContext={ctx} />)
          expect(host.textContent).not.toContain(privateText)
          await fish()
          if (enabled) { await note('client safe'); await click(button(l.add)) }
          else expect(host.querySelector('textarea')).toBeNull()
          await expand()
          await click(button(l.place))
          expect(host.textContent).not.toContain(privateText)
          expect(sent[sent.length - 1]).toEqual([{ item: 'Fish', qty: 1, ...(enabled ? { comment: 'client safe' } : {}) }])
          await reload(<Layout initialContext={ctx} />)
          expect(host.textContent).not.toContain(privateText)
          expect(host.querySelector('textarea')).toBeNull()
        }
      })
      it('keeps disabled notes out of the UI/payload and groups repeat plain dishes', async () => {
        serverOrder = privateOrder
        const ctx = { ...context, capabilities: { ...context.capabilities, item_notes_enabled: false } }
        await render(<Layout initialContext={ctx} />)
        expect(host.textContent).not.toContain(privateText)
        await fish()
        await fish()
        expect(host.querySelector('textarea')).toBeNull()
        await click(button(l.place))
        expect(sent[0]).toEqual([{ item: 'Fish', qty: 2 }])
        expect(host.textContent).not.toContain(privateText)
      })
      it('excludes off-menu/disabled add-ons, blocks unknown prices, accepts genuine zero and uses server menu prices', async () => {
        menu[1] = item('Sauce', null)
        addons = ['Sauce', 'Closed', 'Offmenu', 'Free'].map(code => ({ item_code: code, item_name: code, rate: null }))
        await render(<Layout initialContext={context} />)
        const sauce = button(l.addon)
        expect(sauce.disabled, 'unknown price must not become a free order').toBe(true)
        expect(host.textContent).toContain(l.unavailable)
        expect(host.querySelector('button[aria-label$="Closed"]')).toBeNull()
        expect(host.textContent).not.toContain('Offmenu')
        await click(sauce)
        expect(host.querySelector('textarea')).toBeNull()
        const free = [...host.querySelectorAll<HTMLButtonElement>('button[aria-label]')].find(b => b.getAttribute('aria-label')?.endsWith('Free'))!
        expect(free, 'actual server zero rate remains available').toBeDefined()
        expect(free.disabled).toBe(false)
        await click(free)
        await click(button(l.add.replace('Fish', 'Free')))
        await click(button(l.place))
        expect(sent[0]).toEqual([{ item: 'Free', qty: 1 }])
        menu[1] = item('Sauce', 2000)
        await reload(<Layout initialContext={context} />)
        expect(button(l.addon).textContent).toMatch(/2[,.]?000/)
        await click(button(l.addon))
        await click(button(l.add.replace('Fish', 'Sauce')))
        await click(button(l.place))
        expect(sent[1]).toEqual([{ item: 'Sauce', qty: 1 }])
        expect(host.textContent).toContain('12000') // returned server total, not the cart estimate
      })
      it('shows a localized 503 recovery and retries without losing notes/cart; successful empty is not failure', async () => {
        productFails = true
        await render(<Layout initialContext={context} />)
        expect(host.textContent, '503 cannot masquerade as empty add-ons').toContain(l.error)
        await fish()
        await note('keep this note')
        await click(button(l.add))
        productFails = false
        await click(button(l.retry))
        expect(button(l.addon).disabled).toBe(false)
        await expand()
        expect(host.textContent).toContain('keep this note')
        // Other products also failed; their individual recovery remains visible.
        await reload(<Layout initialContext={context} />)
        expect(host.textContent).not.toContain(l.error)
        expect([...host.querySelectorAll('button')].some(b => b.textContent?.trim() === l.retry)).toBe(false)
        addons = []
        await reload(<Layout initialContext={context} />)
        expect(host.textContent).not.toContain(l.error)
        expect(host.querySelector('button[aria-label*="Sauce"]')).toBeNull()
      })
    })
  }
}

it('keeps every layout menu and the authorized order usable while one optional add-on read stays pending', async () => {
  const quiet = { ...context, capabilities: { ...context.capabilities, item_notes_enabled: false } }
  const served: CustomerOrder = {
    invoice: 'TEST-1', billed: false, grand_total: 9000,
    items: [{ item_code: 'Served', item_name: 'Served', qty: 1, rate: 9000, amount: 9000, comment: privateText }],
  }
  for (const [layoutName, Layout] of Object.entries({ Mobile, Tablet, Landscape, Portrait })) {
    holdItem = 'Fish'
    heldProductReads = 0
    held = []
    addons = [{ item_code: 'Sauce', item_name: 'Sauce', rate: null }]
    serverOrder = served
    sent = []
    await reload(<Layout initialContext={quiet} />)
    expect(host.textContent, `${layoutName} must not stay on the global menu loader`).not.toContain('Loading menu')
    const fishButton = dish('Fish')
    expect(fishButton, `${layoutName} menu stays reachable`).toBeDefined()
    expect(fishButton!.disabled, `${layoutName} priced dish stays orderable`).toBe(false)
    expect(host.textContent, `${layoutName} loads the current order without waiting for add-ons`).toContain('Served')
    expect(host.textContent, `${layoutName} still hides raw staff notes`).not.toContain(privateText)
    expect(fishButton!.parentElement?.textContent, `${layoutName} shows the pending add-on on that dish`).toContain('Loading add-ons')
    expect(fishButton!.parentElement?.querySelector('button[aria-label="Add add-on Sauce"]'), `${layoutName} cannot select an add-on whose price is still unknown`).toBeNull()
    expect(dish('Sauce')!.disabled, `${layoutName} dishes without a pending add-on stay orderable`).toBe(false)
    await click(dish('Free')!)
    await expand()
    await click(button('Place Order'))
    expect(sent, `${layoutName} submits the priced dish and not the unresolved add-on`).toEqual([[{ item: 'Free', qty: 1 }]])
    expect(host.textContent).not.toContain(privateText)
    const pending = held[0]
    expect(pending, `${layoutName} left the optional add-on read pending`).toBeDefined()
    await act(async () => { pending.reject(new Error('503 synthetic add-on outage')) })
    expect(host.textContent, `${layoutName} keeps the menu when the pending add-on fails`).not.toContain('Loading menu')
    expect(dish('Fish'), `${layoutName} menu remains after add-on failure`).toBeDefined()
    expect(host.textContent).toContain('Could not load add-ons.')
    expect(button('Retry add-ons')).toBeDefined()
    expect(host.querySelector('button[aria-label="Add add-on Sauce"]')).toBeNull()
  }

  holdItem = 'Fish'
  heldProductReads = 0
  held = []
  addons = [{ item_code: 'Sauce', item_name: 'Sauce', rate: null }]
  serverOrder = served
  await reload(<Mobile initialContext={quiet} />)
  expect(host.textContent, 'Mobile must not stay on the global menu loader').not.toContain('Loading menu')
  expect(dish('Fish'), 'Mobile menu stays reachable before the session changes').toBeDefined()
  addons = []
  serverOrder = {
    invoice: 'TEST-2', billed: false, grand_total: 1000,
    items: [{ item_code: 'Second', item_name: 'Second', qty: 1, rate: 1000, amount: 1000, comment: privateText }],
  }
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  await click(button('Start Over'))
  expect(host.textContent, 'replacement session shows its own order').toContain('Second')
  expect(host.textContent, 'replacement session drops the previous order').not.toContain('Served')
  expect(host.querySelector('button[aria-label="Add add-on Sauce"]')).toBeNull()
  const late = held[0]
  expect(late, 'the replaced session still has its original add-on read in flight').toBeDefined()
  await act(async () => { late.resolve() })
  expect(host.querySelector('button[aria-label="Add add-on Sauce"]'), 'a late add-on result from the replaced session cannot be selected').toBeNull()
  expect(host.textContent).toContain('Second')
  expect(host.textContent).not.toContain('Served')
  expect(host.textContent).not.toContain(privateText)
  expect(host.textContent).not.toContain('Loading menu')
})

it('keeps nine stalled optional reads from blocking the required order or a guest action', async () => {
  // Chrome allows six HTTP/1.1 connections per origin. Nine hung add-on reads must not take them before the order.
  const browserHttp1Pool = 6
  const quiet = { ...context, capabilities: { ...context.capabilities, item_notes_enabled: false } }
  const served: CustomerOrder = {
    invoice: 'TEST-1', billed: false, grand_total: 9000,
    items: [{ item_code: 'Served', item_name: 'Served', qty: 1, rate: 9000, amount: 9000, comment: privateText }],
  }
  menu = Array.from({ length: 9 }, (_, index) => item(`Dish${index}`, 1000 + index))
  serverOrder = served
  let openSockets = 0
  const socketWaiters: Array<() => void> = []
  const releaseReads: Array<() => void> = []
  let holdProducts = true
  let productReads = 0
  let orderReads = 0
  async function limited<T>(run: () => Promise<T>): Promise<T> {
    if (openSockets >= browserHttp1Pool) await new Promise<void>(resolve => { socketWaiters.push(resolve) })
    openSockets += 1
    try {
      return await run()
    } finally {
      openSockets -= 1
      socketWaiters.shift()?.()
    }
  }
  // Bill-bearing reads are POST bodies; each still occupies one pooled connection.
  const read = (path: string, params?: Record<string, unknown>) => limited(async () => {
    if (path.endsWith('get_customer_menu')) return { message: { items: menu, name: 'Nine item menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) {
      orderReads += 1
      return { message: serverOrder }
    }
    if (path.endsWith('get_customer_product')) {
      productReads += 1
      const itemCode = String(params?.item_code ?? '')
      if (!holdProducts) return { message: { item_code: itemCode, variants: [], addons: [] } }
      return await new Promise(resolve => {
        releaseReads.push(() => resolve({ message: { item_code: itemCode, variants: [], addons: [] } }))
      })
    }
    throw new Error(`Unexpected read ${path}`)
  })
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw new Error(`Unexpected GET ${path}`) })
  vi.spyOn(call, 'post').mockImplementation((path: string, params?: Record<string, unknown>) => /\.get_customer_[a-z_]+$/.test(path) ? read(path, params) : limited(async () => {
    if (!path.endsWith('add_customer_items')) throw new Error(`Unexpected POST ${path}`)
    sent.push(JSON.parse(String(params?.items)))
    return { message: serverOrder }
  }))
  try {
    for (const [layoutName, Layout] of Object.entries({ Mobile, Tablet, Landscape, Portrait })) {
      // A new page load closes the previous document's connections. Release that page before measuring the next one.
      if (releaseReads.length) {
        holdProducts = false
        const previous = releaseReads.splice(0)
        await act(async () => { previous.forEach(release => release()) })
        holdProducts = true
      }
      productReads = 0
      orderReads = 0
      sent = []
      await reload(<Layout initialContext={quiet} />)
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
      expect(orderReads, `${layoutName} sent the required order while ${productReads} optional reads were in flight`).toBe(1)
      expect(productReads, `${layoutName} keeps optional hydration to one connection`).toBeLessThanOrEqual(1)
      expect(openSockets, `${layoutName} leaves a connection free for a guest action`).toBeLessThanOrEqual(1)
      expect(host.textContent, `${layoutName} must not stay on the global menu loader`).not.toContain('Loading menu')
      expect(host.textContent, `${layoutName} shows the whole menu`).toContain('Dish8')
      expect(host.textContent, `${layoutName} shows the current order`).toContain('Served')
      expect(host.textContent, `${layoutName} still hides raw staff notes`).not.toContain(privateText)
      expect(host.textContent, `${layoutName} does not invent prices`).not.toContain('Price unavailable')
      expect(host.querySelector('button[aria-label^="Add add-on"]'), `${layoutName} cannot select an add-on that has not loaded`).toBeNull()
      const pendingAddons = [...host.querySelectorAll('[role="status"]')].filter(node => node.textContent?.includes('Loading add-ons'))
      expect(pendingAddons, `${layoutName} shows the single in-flight add-on read`).toHaveLength(1)
      await click(dish('Dish0')!)
      await expand()
      await click(button('Place Order'))
      expect(sent, `${layoutName} places the priced dish while the optional read is still stalled`).toEqual([[{ item: 'Dish0', qty: 1 }]])
      expect(productReads, `${layoutName} does not fan out optional reads after the guest action`).toBeLessThanOrEqual(1)
      expect(openSockets, `${layoutName} still has a free connection after the guest action`).toBeLessThanOrEqual(1)
      expect(host.textContent).not.toContain('Loading menu')
      expect(host.textContent).not.toContain(privateText)
    }
  } finally {
    holdProducts = false
    const pending = releaseReads.splice(0)
    await act(async () => { pending.forEach(release => release()) })
  }
})

it('places the priced dish while retried add-ons wait behind one held optional read', async () => {
  // Chrome allows six HTTP/1.1 connections per origin. Five visible retries must not occupy the rest while one add-on read is already held.
  const browserHttp1Pool = 6
  const quiet = { ...context, capabilities: { ...context.capabilities, item_notes_enabled: false } }
  const served: CustomerOrder = {
    invoice: 'TEST-1', billed: false, grand_total: 9000,
    items: [{ item_code: 'Served', item_name: 'Served', qty: 1, rate: 9000, amount: 9000, comment: privateText }],
  }
  menu = Array.from({ length: 9 }, (_, index) => item(`Dish${index}`, 1000 + index))
  serverOrder = served
  let openSockets = 0
  const socketWaiters: Array<() => void> = []
  const releaseReads: Array<() => void> = []
  const failedOnce = new Set<string>()
  let holdSlowReads = true
  let heldOptional = 0
  let productReads = 0
  async function limited<T>(run: () => Promise<T>): Promise<T> {
    if (openSockets >= browserHttp1Pool) await new Promise<void>(resolve => { socketWaiters.push(resolve) })
    openSockets += 1
    try {
      return await run()
    } finally {
      openSockets -= 1
      socketWaiters.shift()?.()
    }
  }
  // Bill-bearing reads are POST bodies; each still occupies one pooled connection.
  const read = (path: string, params?: Record<string, unknown>) => limited(async () => {
    if (path.endsWith('get_customer_menu')) return { message: { items: menu, name: 'Nine item menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: serverOrder }
    if (path.endsWith('get_customer_product')) {
      productReads += 1
      const itemCode = String(params?.item_code ?? '')
      const index = Number(itemCode.slice(4))
      if (index < 5 && !failedOnce.has(itemCode)) {
        failedOnce.add(itemCode)
        throw new Error('503 synthetic add-on outage')
      }
      if (holdSlowReads && index <= 5) {
        heldOptional += 1
        return await new Promise(resolve => {
          releaseReads.push(() => {
            heldOptional -= 1
            resolve({ message: { item_code: itemCode, variants: [], addons: [] } })
          })
        })
      }
      return { message: { item_code: itemCode, variants: [], addons: [] } }
    }
    throw new Error(`Unexpected read ${path}`)
  })
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw new Error(`Unexpected GET ${path}`) })
  vi.spyOn(call, 'post').mockImplementation((path: string, params?: Record<string, unknown>) => /\.get_customer_[a-z_]+$/.test(path) ? read(path, params) : limited(async () => {
    if (!path.endsWith('add_customer_items')) throw new Error(`Unexpected POST ${path}`)
    sent.push(JSON.parse(String(params?.items)))
    return { message: serverOrder }
  }))
  const retryButtons = () => [...host.querySelectorAll('button')].filter(node => node.textContent?.trim() === 'Retry add-ons')
  async function drainOptionalReads() {
    // A previous page keeps its queued reads until they settle. Let them finish before the next layout measures a fresh pool.
    holdSlowReads = false
    for (let turn = 0; turn < 16 && (releaseReads.length > 0 || openSockets > 0); turn += 1) {
      const pending = releaseReads.splice(0)
      await act(async () => { pending.forEach(release => release()) })
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    holdSlowReads = true
  }
  try {
    for (const [layoutName, Layout] of Object.entries({ Mobile, Tablet, Landscape, Portrait })) {
      await drainOptionalReads()
      failedOnce.clear()
      heldOptional = 0
      productReads = 0
      sent = []
      await reload(<Layout initialContext={quiet} />)
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
      expect(retryButtons(), `${layoutName} shows a recovery control for each fast add-on failure`).toHaveLength(5)
      expect(heldOptional, `${layoutName} holds the next optional read and no others`).toBe(1)
      expect(openSockets, `${layoutName} has one optional read on the wire`).toBe(1)
      expect(host.textContent, `${layoutName} must not stay on the global menu loader`).not.toContain('Loading menu')
      expect(host.textContent, `${layoutName} shows the current order before add-on recovery`).toContain('Served')
      expect(host.textContent, `${layoutName} still hides raw staff notes`).not.toContain(privateText)
      expect(dish('Dish8')?.disabled, `${layoutName} priced dish stays orderable`).toBe(false)
      for (let attempt = 0; attempt < 5; attempt += 1) await click(button('Retry add-ons'))
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
      await click(dish('Dish8')!)
      await expand()
      await click(button('Place Order'))
      expect(sent, `${layoutName} placed Dish8 while ${heldOptional} optional reads were held after ${productReads} product reads`).toEqual([[{ item: 'Dish8', qty: 1 }]])
      expect(heldOptional, `${layoutName} keeps retried add-on reads behind the one held request`).toBe(1)
      expect(openSockets, `${layoutName} still has room for a guest action`).toBeLessThanOrEqual(1)
      expect(host.textContent, `${layoutName} does not reveal the raw staff note on submit`).not.toContain(privateText)
      const readsBeforeResume = productReads
      const pending = releaseReads.splice(0)
      expect(pending, `${layoutName} still has the original optional read to resume`).toHaveLength(1)
      await act(async () => { pending[0]() })
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
      expect(productReads, `${layoutName} still performs the queued retry after the held read finishes`).toBeGreaterThan(readsBeforeResume)
      expect(heldOptional, `${layoutName} resumes at most one queued retry`).toBeLessThanOrEqual(1)
    }
  } finally {
    await drainOptionalReads()
  }
})

it('removes unqualified comments from the real hook state on load and submit (not just hidden in CSS)', async () => {
  serverOrder = privateOrder
  const ctx = { ...context, capabilities: { ...context.capabilities, item_notes_enabled: false } }
  function Probe() { hook = useOrderingSession(ctx); return null }
  await render(<Probe />)
  expect.soft(hook.order?.items[0].comment, 'server-private data cannot enter layout state on load').toBeNull()
  await act(async () => hook.addToCart(menu[0] as MenuItem))
  await act(async () => hook.submitCart())
  expect.soft(hook.order?.items[0].comment, 'server-private data cannot enter layout state on submit').toBeNull()
  expect(sent).toEqual([[{ item: 'Fish', qty: 1 }]])
  expect.soft(JSON.stringify(hook.order)).not.toContain(privateText)
})
