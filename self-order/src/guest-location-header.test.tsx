import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import { initI18n } from './i18n'
import Mobile from './layouts/MobileQRLayout'
import Tablet from './layouts/TabletLayout'
import Landscape from './layouts/LandscapeKioskLayout'
import Portrait from './layouts/PortraitKioskLayout'
import type { OrderingContext } from './lib/api'

// Simphony Mobile Order and Pay: every QR code is a location with a subtype (Dine In, Room Service, ...);
// the guest sees that location's own label. The header never prefixes a hard-coded "Table".
const base: OrderingContext = {
  session: 'test-session', restaurant: 'Outlet', table: 'Room 103', source: 'QR Table', layout: 'Mobile',
  capabilities: {
    product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true,
    feedback_enabled: false,
  },
}
let root: Root
let host: HTMLDivElement

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order')
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw new Error(`Unexpected GET ${path}`) })
  vi.spyOn(call, 'post').mockImplementation(async (path: string) => {
    if (path.endsWith('get_customer_menu')) return { message: { items: [], name: 'Test menu', modified_time: null } }
    if (path.endsWith('get_customer_order')) return { message: { invoice: null, items: [], grand_total: 0, billed: false } }
    throw new Error(`Unexpected POST ${path}`)
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  await initI18n('en', { setCookie() {} })
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function heading(Layout: typeof Mobile, context: OrderingContext): Promise<string> {
  await act(async () => { root.render(<Layout initialContext={context} />) })
  const found = host.querySelector('h1')
  expect(found, 'the layout renders its header').not.toBeNull()
  return found!.textContent?.trim() ?? ''
}

for (const [name, Layout] of Object.entries({ Mobile, Tablet, Landscape, Portrait })) {
  describe(`${name} guest header names the location`, () => {
    it('shows the area and the location for a room: "In-Room Dining · Room 103"', async () => {
      const text = await heading(Layout, { ...base, area: 'In-Room Dining' })
      expect(text).toBe('In-Room Dining · Room 103')
      expect(text).not.toContain('Table ')
    })
    it('shows the location alone when the server sends no area', async () => {
      const text = await heading(Layout, { ...base, table: 'T-12' })
      expect(text).toBe('T-12')
      expect(text).not.toContain('Table ')
    })
    for (const lang of ['sw', 'lg'] as const) {
      it(`keeps the names untranslated in ${lang}`, async () => {
        await initI18n(lang, { setCookie() {} })
        expect(await heading(Layout, { ...base, area: 'In-Room Dining' })).toBe('In-Room Dining · Room 103')
      })
    }
  })
}

describe('Mobile header without a location is unchanged', () => {
  it('pickup still reads the pickup title', async () => {
    expect(await heading(Mobile, { ...base, table: null, source: 'QR Pickup' })).toBe('Order for Pickup')
  })
  it('no table and no pickup reads Order', async () => {
    expect(await heading(Mobile, { ...base, table: null })).toBe('Order')
  })
})
