import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import App from '../../self-order/src/App'
import { initI18n } from '../../self-order/src/i18n'
import LandscapeKioskLayout from '../../self-order/src/layouts/LandscapeKioskLayout'
import MobileQRLayout from '../../self-order/src/layouts/MobileQRLayout'
import PortraitKioskLayout from '../../self-order/src/layouts/PortraitKioskLayout'
import TabletLayout from '../../self-order/src/layouts/TabletLayout'
import type { OrderingContext } from '../../self-order/src/lib/api'

vi.mock('../../self-order/src/hooks/useDeviceBootstrap', () => ({
  useDeviceBootstrap: () => ({
    isDevice: true,
    deviceContext: null,
    deviceLoading: false,
    deviceError: 'This device could not be started. Please contact staff.',
  }),
}))

const context: OrderingContext = {
  session: 'synthetic-session',
  restaurant: 'Restaurant A',
  table: 'T-07',
  source: 'QR Table',
  layout: 'Mobile',
  capabilities: {
    product_detail_enabled: true,
    show_item_images: false,
    show_item_descriptions: false,
    item_notes_enabled: false,
    request_bill_enabled: true,
    customer_payment_enabled: false,
    payment_link_enabled: false,
    pay_at_counter_enabled: true,
    add_to_running_table_enabled: true,
  },
}

const order = {
  invoice: 'POS-1',
  items: [{ item_code: 'Soup', item_name: 'Soup', qty: 1, rate: 12000, amount: 12000 }],
  grand_total: 12000,
  billed: false,
}

let root: Root
let container: HTMLDivElement

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  window.history.replaceState(null, '', '/order')
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => ({
    message: String(path).endsWith('get_customer_menu')
      ? {
          items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0, item_image: null, course: null, course_label: null }],
          modified_time: null,
          name: 'Menu A',
        }
      : order,
  }))
  vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'Requested', request: 'REQ-1' } })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await initI18n('en', { setCookie() {} })
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function button(label: string) {
  return [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim() === label)
}

async function render(node: React.ReactNode) {
  await act(async () => {
    root.render(node)
  })
  await act(async () => {
    await Promise.resolve()
  })
}

describe('guest language switch', () => {
  it('keeps the pending cart and shows the new language', async () => {
    await render(<MobileQRLayout initialContext={context} />)
    const soup = [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.includes('Soup'))
    expect(soup).toBeDefined()
    await act(async () => soup!.click())
    expect(container.textContent).toContain('Soup')
    expect(button('Place Order')).toBeDefined()
    const luganda = button('Luganda')
    expect(luganda).toBeDefined()
    await act(async () => {
      luganda!.click()
    })
    expect(container.textContent).toContain('Soup')
    expect(button('Teeka oda')).toBeDefined()
    expect(container.textContent).toContain('Mu kikapu: 1')
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
    await render(<TabletLayout initialContext={{ ...context, layout: 'Tablet' }} />)
    await act(async () => {
      vi.advanceTimersByTime(60000)
    })
    const close = container.querySelector('button[aria-label]')
    expect(close?.getAttribute('aria-label')).toBe('Funga')
    expect(close?.textContent).toContain('Funga')
  })
})
