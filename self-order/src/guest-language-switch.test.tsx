import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import App from './App'
import LandscapeKioskLayout from './layouts/LandscapeKioskLayout'
import MobileQRLayout from './layouts/MobileQRLayout'
import PortraitKioskLayout from './layouts/PortraitKioskLayout'
import TabletLayout from './layouts/TabletLayout'
import type { OrderingContext } from './lib/api'

// Loaded lazily: without the guest i18n module the page stays as it is, so each
// test fails on its own assertion about the rendered page.
async function initI18n(lang: string, env: { setCookie(value: string): void }) {
  const specifier = './i18n'
  const i18n = await import(/* @vite-ignore */ specifier).catch(() => null)
  return i18n?.initI18n(lang, env)
}

// Ported from the fork's pos/tests/guest-language-switch.test.tsx (#16) onto the
// v16 screens: the cart and Place Order live on the checkout screen here.
vi.mock('./hooks/useDeviceBootstrap', () => ({
  useDeviceBootstrap: () => ({
    isDevice: true,
    deviceContext: null,
    deviceLoading: false,
    deviceError: 'This device could not be started. Please contact staff.',
  }),
}))
vi.mock('./lib/realtime', () => ({ useMenuAvailabilityChannel: () => {} }))

const context: OrderingContext = {
  session: 'synthetic-session',
  source: 'QR Table',
  restaurant: 'Restaurant A',
  // No company: the display-only availability lookup stays off in these tests.
  company: '',
  table: 'T-07',
  layout: 'Mobile',
  capabilities: {
    product_detail_enabled: false,
    show_item_images: false,
    show_item_descriptions: false,
    item_notes_enabled: false,
    request_bill_enabled: true,
    customer_payment_enabled: false,
    payment_link_enabled: false,
    pay_at_counter_enabled: true,
    add_to_running_table_enabled: true,
  },
  session_idle_timeout_minutes: 30,
}

const menu = {
  items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0, item_image: null, course: null, course_label: null }],
  modified_time: null,
  name: 'Menu A',
}
const order = {
  invoice: 'POS-1',
  items: [{ item_code: 'Soup', item_name: 'Soup', qty: 1, comment: null, rate: 12000, amount: 12000 }],
  grand_total: 12000,
  billed: false,
}

let root: Root
let container: HTMLDivElement
// Bill-bearing guest reads travel as POST bodies; guest actions use `writes`.
let writes: ReturnType<typeof vi.fn>

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order')
  writes = vi.fn().mockResolvedValue({ message: { status: 'Requested', request: 'REQ-1' } })
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => {
    throw Error(`Unexpected GET ${path}`)
  })
  vi.spyOn(call, 'post').mockImplementation(async (path: string, args?: Record<string, unknown>) => {
    if (String(path).endsWith('get_customer_menu')) return { message: menu }
    if (String(path).endsWith('get_customer_order')) return { message: order }
    return writes(path, args)
  })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await initI18n('en', { setCookie() {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  document.getElementById('guest-footer')?.remove()
  await initI18n('en', { setCookie() {} })
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function button(label: string) {
  return [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim() === label)
}
function buttonStarting(prefix: string) {
  return [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim().startsWith(prefix))
}

async function render(node: React.ReactNode) {
  await act(async () => {
    root.render(node)
  })
  await act(async () => {
    await Promise.resolve()
  })
}

async function click(target: HTMLElement | undefined, name: string) {
  expect(target, name).toBeDefined()
  await act(async () => {
    target!.click()
  })
}

async function addSoupAndCheckout() {
  await click(buttonStarting('Soup'), 'Soup card')
  await click(buttonStarting('View Cart'), 'cart bar')
  await click(button('Proceed to checkout'), 'Proceed to checkout')
}

describe('guest language switch', () => {
  it('keeps the pending cart and shows the new language', async () => {
    await render(<MobileQRLayout initialContext={context} />)
    await click(buttonStarting('Soup'), 'Soup card')
    expect(container.textContent).toContain('In cart: 1')
    await click(button('Luganda'), 'Luganda')
    expect(container.textContent).toContain('Mu kikapu: 1')
    expect(container.textContent).not.toContain('In cart: 1')
    await click(buttonStarting('View Cart'), 'cart bar')
    await click(button('Proceed to checkout'), 'Proceed to checkout')
    expect(button('Teeka oda')).toBeDefined()
    expect(button('Place Order')).toBeUndefined()
  })

  it('gives every language a 44px target', async () => {
    await render(<MobileQRLayout initialContext={context} />)
    for (const label of ['EN', 'Kiswahili', 'Luganda']) {
      const target = button(label) as HTMLButtonElement | undefined
      expect(target, label).toBeDefined()
      expect(Number.parseFloat(target!.style.minHeight), label).toBeGreaterThanOrEqual(44)
      expect(Number.parseFloat(target!.style.minWidth), label).toBeGreaterThanOrEqual(44)
    }
  })

  it.each([
    ['mobile', MobileQRLayout],
    ['tablet', TabletLayout],
    ['landscape', LandscapeKioskLayout],
    ['portrait', PortraitKioskLayout],
  ])('offers the language switcher when %s has no ordering code', async (_name, Layout) => {
    await render(<Layout />)
    expect(button('EN')).toBeDefined()
    expect(button('Kiswahili')).toBeDefined()
    expect(button('Luganda')).toBeDefined()
  })

  it('offers the language switcher when the device cannot start', async () => {
    await render(<App />)
    expect(button('Kiswahili')).toBeDefined()
    expect(button('Luganda')).toBeDefined()
  })

  it('translates the idle dialog close control', async () => {
    await initI18n('sw', { setCookie() {} })
    vi.useFakeTimers()
    await render(<TabletLayout initialContext={{ ...context, layout: 'Tablet', session_idle_timeout_minutes: 1 }} />)
    await act(async () => {
      vi.advanceTimersByTime(60000)
    })
    const close = container.querySelector('[role="dialog"] button[aria-label]')
    expect(close?.getAttribute('aria-label')).toBe('Funga')
    expect(close?.textContent).toContain('Funga')
    expect(container.textContent).toContain('Bado uko?')
  })

  it('translates a recovery error that is already on screen', async () => {
    await render(<MobileQRLayout />)
    expect(container.textContent).toContain('This link is missing an ordering code. Please rescan the QR code on your table.')
    await click(button('Luganda'), 'Luganda')
    expect(container.textContent).toContain("Link eno terina koodi y'oda. Nsaba oskane nate koodi ya QR eri ku mmeeza.")
    expect(container.textContent).not.toContain('This link is missing an ordering code')
    await click(button('Kiswahili'), 'Kiswahili')
    expect(container.textContent).toContain('Kiungo hiki hakina msimbo wa oda. Tafadhali skani tena msimbo wa QR ulio mezani.')
    expect(container.textContent).not.toContain("Link eno terina koodi y'oda")
  })

  it('translates a waiter confirmation that is already on screen', async () => {
    await render(<MobileQRLayout initialContext={context} />)
    await click(button('Call waiter'), 'Call waiter')
    expect(writes).toHaveBeenCalledWith('ury.ury.api.self_ordering.request_assistance', { session: 'synthetic-session' })
    expect(container.textContent).toContain('Waiter requested — staff notified')
    expect(button('Call waiter')?.disabled).toBe(true)
    await click(button('Kiswahili'), 'Kiswahili')
    expect(container.textContent).toContain('Mhudumu ameombwa — wahudumu wamearifiwa')
    expect(container.textContent).not.toContain('Waiter requested — staff notified')
    expect(button('Ita mhudumu')?.disabled).toBe(true)
    await click(button('Luganda'), 'Luganda')
    expect(container.textContent).toContain('Omuweereza asabiddwa — abaweereza bategeezeddwa')
    expect(button('Yita omuweereza')?.disabled).toBe(true)
  })

  it('translates an already-requested waiter confirmation that is already on screen', async () => {
    writes.mockResolvedValue({ message: { status: 'Already Requested', request: 'REQ-1' } })
    await render(<MobileQRLayout initialContext={context} />)
    await click(button('Call waiter'), 'Call waiter')
    expect(container.textContent).toContain('Waiter already requested — staff notified')
    await click(button('Luganda'), 'Luganda')
    expect(container.textContent).toContain('Omuweereza yali asabiddwa — abaweereza bategeezeddwa')
    expect(container.textContent).not.toContain('Waiter already requested')
    expect(button('Yita omuweereza')?.disabled).toBe(true)
  })

  it('translates a client order error that is already on screen and keeps the cart', async () => {
    writes.mockRejectedValue({ httpStatus: 503 })
    await render(<MobileQRLayout initialContext={context} />)
    await addSoupAndCheckout()
    await click(button('Place Order'), 'Place Order')
    expect(container.textContent).toContain('Could not place the order. Please try again.')
    await click(button('Kiswahili'), 'Kiswahili')
    expect(container.textContent).toContain('Imeshindwa kuweka oda. Tafadhali jaribu tena.')
    expect(container.textContent).not.toContain('Could not place the order. Please try again.')
    expect(button('Weka oda')).toBeDefined()
  })

  it('keeps the server refusal text when the language changes', async () => {
    writes.mockRejectedValue({
      _server_messages: JSON.stringify([
        JSON.stringify({ message: '[QR-WAITER-OPENS-TABLE] Please ask a waiter to open your table first.' }),
      ]),
    })
    await render(<MobileQRLayout initialContext={context} />)
    await addSoupAndCheckout()
    await click(button('Place Order'), 'Place Order')
    expect(container.textContent).toContain('Please ask a waiter to open your table first.')
    expect(container.textContent).not.toContain('[QR-WAITER-OPENS-TABLE]')
    await click(button('Kiswahili'), 'Kiswahili')
    expect(container.textContent).toContain('Please ask a waiter to open your table first.')
    expect(container.textContent).not.toContain('Imeshindwa kuweka oda')
    expect(button('Weka oda')).toBeDefined()
  })

  it('updates static guest copy that is already on the page when the language changes', async () => {
    const footer = document.createElement('footer')
    footer.id = 'guest-footer'
    footer.innerHTML = '<span data-guest-i18n="footer.built_on">built on</span> URY · <a href="#" data-guest-i18n="footer.source">Source</a>'
    document.body.appendChild(footer)
    await render(<MobileQRLayout initialContext={context} />)
    await click(button('Kiswahili'), 'Kiswahili')
    expect(footer.textContent).toContain('imejengwa kwa')
    expect(footer.textContent).toContain('Chanzo')
    await click(button('Luganda'), 'Luganda')
    expect(footer.textContent).toContain('ezimbiddwa ku')
    expect(footer.textContent).toContain('Ensibuko')
    await click(button('EN'), 'EN')
    expect(footer.textContent).toContain('built on')
    expect(footer.textContent).toContain('Source')
    expect(document.documentElement.lang).toBe('en')
  })
})
