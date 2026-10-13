import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import MobileQRLayout from '../../self-order/src/layouts/MobileQRLayout'
import { initI18n } from '../../self-order/src/i18n'
import type { OrderingContext } from '../../self-order/src/lib/api'

const context: OrderingContext = {
  session: 'session-a', restaurant: 'Restaurant A', table: 'T-05', source: 'QR Table', layout: 'Mobile',
  capabilities: { product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true },
}
const menu = { items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0,
  item_image: null, course: null, course_label: null }], modified_time: null, name: 'Menu A' }
const order = { invoice: 'POS-1', items: [{ item_code: 'Soup', item_name: 'Soup', qty: 2,
  comment: null, rate: 12000, amount: 24000 }], grand_total: 24000, billed: false }
const emptyOrder = { invoice: null, items: [], grand_total: 0, billed: false }
let root: Root
let container: HTMLDivElement
let bootstraps: number

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order?t=qr-a')
  bootstraps = 0
  vi.spyOn(call, 'get').mockImplementation(async (path: string, args?: Record<string, unknown>) => {
    if (path.endsWith('get_ordering_context')) {
      bootstraps++
      return { message: bootstraps === 1 ? context : { ...context, session: 'session-b', table: 'T-06' } }
    }
    if (path.endsWith('get_customer_menu')) return { message: menu }
    if (path.endsWith('get_customer_order')) return { message: args?.session === 'session-a' ? order : emptyOrder }
    throw Error(`Unexpected read ${path}`)
  })
  vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'Requested', request: 'REQ-1' } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await initI18n('en', { setCookie() {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  await initI18n('en', { setCookie() {} })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function render() { await act(async () => root.render(<MobileQRLayout />)) }
async function reload() {
  await act(async () => root.unmount())
  root = createRoot(container)
  await render()
}
function button(label: string) {
  return [...container.querySelectorAll('button')].find(candidate => candidate.textContent?.trim() === label)
}

describe('guest reload', () => {
  it('restores the running order from the same server session with the QR still in the URL', async () => {
    await render()
    await reload()
    expect(container.textContent).toContain('Your order so far')
    expect(container.textContent).toContain('Soup × 2')
    expect(container.textContent).toContain('24000')
    expect(call.get).toHaveBeenLastCalledWith('ury.ury.api.self_ordering.get_customer_order', { session: 'session-a' })
    expect(bootstraps).toBe(1)
  })

  it('restores Call waiter and sends the resumed session', async () => {
    await render()
    await reload()
    expect(button('Call waiter')).toBeDefined()
    await act(async () => button('Call waiter')!.click())
    expect(container.textContent).toContain('Waiter requested — staff notified')
    expect(call.post).toHaveBeenCalledWith('ury.ury.api.self_ordering.request_assistance', { session: 'session-a' })
  })

  it('does not reuse the previous table session when another QR is opened', async () => {
    await render()
    window.history.replaceState(null, '', '/order?t=qr-b')
    await reload()
    expect(container.textContent).toContain('Table T-06')
    expect(container.textContent).not.toContain('Your order so far')
    expect(bootstraps).toBe(2)
  })

  it('continues to resume a tokenless page from its saved context', async () => {
    await render()
    window.history.replaceState(null, '', '/order')
    await reload()
    expect(container.textContent).toContain('Your order so far')
    expect(button('Call waiter')).toBeDefined()
    expect(bootstraps).toBe(1)
  })

  it.each([
    ['en', 'expired', 'Ordering session expired. Please rescan the QR code.'],
    ['en', 'closed', 'Ordering session expired or invalid. Please rescan the QR code.'],
    ['sw', 'expired', 'Kipindi cha kuagiza kimeisha. Tafadhali skani tena msimbo wa QR.'],
    ['sw', 'closed', 'Kipindi cha kuagiza kimeisha au si halali. Tafadhali skani tena msimbo wa QR.'],
    ['lg', 'expired', 'Ekiseera ky’okulagira kiweddeko. Nsaba oskane nate koodi ya QR.'],
    ['lg', 'closed', 'Ekiseera ky’okulagira kiweddeko oba si kituufu. Nsaba oskane nate koodi ya QR.'],
  ])('shows the ERP sentence without cached table/order UI after a %s %s reload', async (language, _status, sentence) => {
    await initI18n(language, { setCookie() {} })
    await render()
    const get = vi.mocked(call.get).getMockImplementation()!
    vi.mocked(call.get).mockImplementation(async (path, args) => {
      if (path.endsWith('get_customer_order')) throw {
        httpStatus: 403, exc_type: 'PermissionError',
        _server_messages: JSON.stringify([JSON.stringify({ message: sentence })]),
      }
      return get(path, args)
    })
    await reload()
    expect.soft(container.textContent).toContain(sentence)
    expect.soft(container.querySelector('header')).toBeNull()
    expect(container.textContent).not.toContain('T-05')
    expect(container.textContent).not.toContain('Soup')
    expect(bootstraps).toBe(1)
  })

  it('keeps the translated transport fallback without a cached table on a failed reload', async () => {
    await initI18n('sw', { setCookie() {} })
    await render()
    const get = vi.mocked(call.get).getMockImplementation()!
    vi.mocked(call.get).mockImplementation(async (path, args) => {
      if (path.endsWith('get_customer_order')) throw { httpStatus: 503 }
      return get(path, args)
    })
    await reload()
    expect(container.textContent).toContain('Imeshindwa kupakia menyu. Tafadhali skani tena msimbo wa QR.')
    expect(container.querySelector('header')).toBeNull()
  })
})

describe('translated ERP refusals', () => {
  const refusals = [
    ['en', 'Please ask a waiter to open your table first.', 'Place Order'],
    ['sw', 'Tafadhali mwombe mhudumu afungue meza yako kwanza.', 'Weka oda'],
    ['lg', 'Nsaba osabe omuweereza aggulawo emmeeza yo okusooka.', 'Teeka oda'],
  ]
  it.each(refusals)('shows the %s server sentence once without the legacy wrapper', async (language, sentence, placeOrder) => {
    await initI18n(language, { setCookie() {} })
    vi.mocked(call.post).mockRejectedValue({ _server_messages: JSON.stringify([
      JSON.stringify({ message: `[SAL-QR-WAITER-OPENS-TABLE] ${sentence}` }),
      JSON.stringify({ message: `Error while placing order: [SAL-QR-WAITER-OPENS-TABLE] ${sentence}` }),
    ]) })
    await render()
    await act(async () => [...container.querySelectorAll('button')].find(candidate => candidate.textContent?.startsWith('Soup'))!.click())
    await act(async () => button(placeOrder)!.click())
    expect(container.textContent!.split(sentence)).toHaveLength(2)
    expect(container.textContent).not.toContain('Error while placing order')
    expect(container.textContent).not.toContain('SAL-QR')
    expect(button(placeOrder)).toBeDefined()
  })

  it.each(refusals)('removes the legacy wrapper from an exception-only %s refusal', async (language, sentence, placeOrder) => {
    await initI18n(language, { setCookie() {} })
    vi.mocked(call.post).mockRejectedValue({ exception: `frappe.exceptions.ValidationError: Error while placing order: [SAL-QR-WAITER-OPENS-TABLE] ${sentence}` })
    await render()
    await act(async () => [...container.querySelectorAll('button')].find(candidate => candidate.textContent?.startsWith('Soup'))!.click())
    await act(async () => button(placeOrder)!.click())
    expect(container.textContent).toContain(sentence)
    expect(container.textContent).not.toContain('Error while placing order')
  })

  it('preserves distinct validation reasons rather than taking only the first message', async () => {
    vi.mocked(call.post).mockRejectedValue({ _server_messages: JSON.stringify([
      JSON.stringify({ message: 'Please ask staff.' }), JSON.stringify({ message: 'Your bill is printed.' }),
    ]) })
    await render()
    await act(async () => [...container.querySelectorAll('button')].find(candidate => candidate.textContent?.startsWith('Soup'))!.click())
    await act(async () => button('Place Order')!.click())
    expect(container.textContent).toContain('Please ask staff.')
    expect(container.textContent).toContain('Your bill is printed.')
  })
})
