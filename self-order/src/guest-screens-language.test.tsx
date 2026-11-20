import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import MobileQRLayout from './layouts/MobileQRLayout'
import type { OrderingContext } from './lib/api'

// Review B2 on #113: a Swahili or Luganda guest walks every Mobile screen
// (menu, product detail, cart page, checkout, order status) and sees no English.
async function initI18n(lang: string) {
  const specifier = './i18n'
  const i18n = await import(/* @vite-ignore */ specifier).catch(() => null)
  return i18n?.initI18n(lang, { setCookie() {} })
}

vi.mock('./lib/realtime', () => ({ useMenuAvailabilityChannel: () => {} }))

const context: OrderingContext = {
  session: 'session-a', source: 'QR Table', restaurant: 'Restaurant A', company: '', table: 'T-05', layout: 'Mobile',
  capabilities: { product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: true, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true },
  session_idle_timeout_minutes: 30,
}
const menu = { items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0,
  item_image: null, course: null, course_label: null }], modified_time: null, name: 'Menu A' }
const placed = { invoice: 'POS-1', items: [{ item_code: 'Soup', item_name: 'Soup', qty: 1,
  comment: null, rate: 12000, amount: 12000 }], grand_total: 12000, billed: false }
const status = { session_status: 'Active', invoice: 'POS-1', billed: false, submitted: true,
  open_requests: [{ name: 'REQ-1', request_type: 'Assistance', status: 'Open' }] }
const product = { item_code: 'Soup', item_name: 'Soup', description: null, image: null,
  variants: [{ item_code: 'Soup-L', item_name: 'Soup Large', rate: 14000 }],
  addons: [{ item_code: 'Bread', item_name: 'Bread', rate: 2000 }] }

// English the guest must not see; ERP names (Soup, Bread, T-05) are data, never translated.
const ENGLISH = [
  'Back to menu', 'Your Cart', 'Your cart is empty', 'Browse the menu', 'Proceed to checkout', 'Note:',
  'Back to cart', 'Checkout', 'Order Summary', 'Payment Method', 'Pay at Counter', 'View Cart',
  'Order confirmed', 'Thanks', 'Pickup code', 'Order status', 'Submitted', 'Bill status', 'Not billed yet',
  'Open service requests', 'Assistance', 'Add more items', 'Done',
  'Variants', 'Add-ons', 'Add to cart', 'Back', 'Loading item',
  ' item', ' items',
]
const SEARCH_ENGLISH = ['Search menu', 'Clear search']

let root: Root
let container: HTMLDivElement

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order')
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw Error(`Unexpected GET ${path}`) })
  let order: unknown = { invoice: null, items: [], grand_total: 0, billed: false }
  vi.spyOn(call, 'post').mockImplementation(async (path: string) => {
    if (path.endsWith('get_customer_menu')) return { message: menu }
    if (path.endsWith('get_customer_order')) return { message: order }
    if (path.endsWith('add_customer_items')) { order = placed; return { message: placed } }
    if (path.endsWith('get_order_status')) return { message: status }
    if (path.endsWith('get_customer_product')) return { message: product }
    throw Error(`Unexpected POST ${path}`)
  })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  await initI18n('en')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await Promise.resolve() })
}
async function render(ctx: OrderingContext) {
  await act(async () => root.render(<MobileQRLayout initialContext={ctx} />))
  await settle()
}
function buttonStarting(prefix: string) {
  return [...container.querySelectorAll('button')].find(candidate => candidate.textContent?.trim().startsWith(prefix))
}
async function click(target: Element | null | undefined, name: string) {
  expect(target, name).toBeTruthy()
  await act(async () => (target as HTMLElement).click())
  await settle()
}
function visibleCopy(): string {
  const attrs = [...container.querySelectorAll('[aria-label],[placeholder],[title]')]
    .flatMap(node => ['aria-label', 'placeholder', 'title'].map(name => node.getAttribute(name) ?? ''))
  return [container.textContent ?? '', ...attrs].join('\n')
}
function expectNoEnglish(screen: string) {
  const copy = visibleCopy()
  const left = [...ENGLISH, ...SEARCH_ENGLISH].filter(phrase => copy.includes(phrase))
  expect(left, `${screen} still shows English`).toEqual([])
}

describe.each([
  ['sw', { cart: 'Kikapu chako', checkout: 'Kamilisha oda', confirmed: 'Oda imethibitishwa', search: 'Tafuta kwenye menyu...', addToCart: 'Weka kikapuni' }],
  ['lg', { cart: 'Ekikapu kyo', checkout: 'Malirizza oda', confirmed: 'Oda ekakasiddwa', search: 'Noonya ku menyu...', addToCart: 'Teeka mu kikapu' }],
])('a %s guest sees every Mobile screen in their language', (language, copy) => {
  it('menu, cart page, checkout and order status', async () => {
    await initI18n(language)
    await render(context)
    expect(container.querySelector('input')?.getAttribute('placeholder')).toBe(copy.search)
    expectNoEnglish('menu')

    await click(buttonStarting('Soup'), 'Soup card')
    expectNoEnglish('menu with a cart')
    await click(container.querySelector('.fixed button'), 'cart bar')
    expect(container.querySelector('h1')?.textContent).toBe(copy.cart)
    expectNoEnglish('cart page')

    await click(container.querySelector('.border-t button'), 'proceed to checkout')
    expect(container.querySelector('h1')?.textContent).toBe(copy.checkout)
    expectNoEnglish('checkout')

    const place = [...container.querySelectorAll('button')].at(-1)
    await click(place, 'place order')
    expect(container.querySelector('h1')?.textContent).toBe(copy.confirmed)
    expectNoEnglish('order status')
  })

  it('product detail', async () => {
    await initI18n(language)
    await render({ ...context, capabilities: { ...context.capabilities, product_detail_enabled: true } })
    await click(buttonStarting('Soup'), 'Soup card')
    expect(container.textContent).toContain('Soup Large')
    expect([...container.querySelectorAll('button')].some(b => b.textContent?.trim() === copy.addToCart)).toBe(true)
    expectNoEnglish('product detail')
  })

  it('an empty cart page', async () => {
    await initI18n(language)
    await render(context)
    await click(buttonStarting('Soup'), 'Soup card')
    await click(container.querySelector('.fixed button'), 'cart bar')
    await click([...container.querySelectorAll('button')].find(b => b.getAttribute('aria-label')?.length && b.textContent?.trim() === '−'), 'remove one')
    expectNoEnglish('empty cart page')
  })
})
