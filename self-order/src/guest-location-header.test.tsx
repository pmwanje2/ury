import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import Mobile from './layouts/MobileQRLayout'
import Tablet from './layouts/TabletLayout'
import Landscape from './layouts/LandscapeKioskLayout'
import Portrait from './layouts/PortraitKioskLayout'
import type { OrderingContext } from './lib/api'

// Loaded lazily: without the guest i18n module the page stays as it is, so each
// test fails on its own assertion about the rendered page.
async function initI18n(lang: string, env: { setCookie(value: string): void }) {
  const specifier = './i18n'
  const i18n = await import(/* @vite-ignore */ specifier).catch(() => null)
  return i18n?.initI18n(lang, env)
}

// Ported from the carrier's self-order/src/guest-location-header.test.tsx (#97, 37b6e9d2).
// Simphony Mobile Order and Pay: every QR code is a location with a subtype (Dine In, Room Service, ...);
// the guest sees that location's own label. The header never prefixes a hard-coded "Table".
vi.mock('./lib/realtime', () => ({ useMenuAvailabilityChannel: () => {} }))

const base: OrderingContext & { area?: string | null } = {
  session: 'test-session', source: 'QR Table', restaurant: 'Outlet', company: '', table: 'Room 103', layout: 'Mobile',
  capabilities: {
    product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true,
  },
  session_idle_timeout_minutes: 30,
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
  await initI18n('en', { setCookie() {} })
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
      const text = await heading(Layout, { ...base, area: 'In-Room Dining' } as OrderingContext)
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
        expect(await heading(Layout, { ...base, area: 'In-Room Dining' } as OrderingContext)).toBe('In-Room Dining · Room 103')
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
