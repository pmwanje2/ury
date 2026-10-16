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

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order')
  menu = [item('Fish', 12000), item('Sauce', 2000), item('Closed', 1000, 1), item('Free', 0)]
  addons = [{ item_code: 'Sauce', item_name: 'Sauce', rate: null }]
  productFails = false
  serverOrder = emptyOrder
  sent = []
  vi.spyOn(call, 'get').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
    if (path.endsWith('get_customer_menu')) return { message: { items: menu, name: 'Test menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: serverOrder }
    if (path.endsWith('get_customer_product')) {
      if (productFails) throw new Error('503 synthetic add-on outage')
      return { message: { item_code: params?.item_code, variants: [], addons: params?.item_code === 'Fish' ? addons : [] } }
    }
    throw new Error(`Unexpected GET ${path}`)
  })
  vi.spyOn(call, 'post').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
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
