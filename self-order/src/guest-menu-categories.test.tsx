import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { call } from '@ury/core'
import { initI18n } from './i18n'
import Mobile from './layouts/MobileQRLayout'
import Tablet from './layouts/TabletLayout'
import Landscape from './layouts/LandscapeKioskLayout'
import Portrait from './layouts/PortraitKioskLayout'
import type { MenuItem, OrderingContext } from './lib/api'

// Oracle Simphony shows a menu as screen look-up groups in the order the menu
// configures. URY's equivalent is URY Menu Course.custom_serving_priority,
// then the URY Menu Item row order. Item names never decide the order.
// Real layouts, hook and i18n; only the HTTP boundary is doubled.
const context: OrderingContext = {
  session: 'test-session', restaurant: 'Test restaurant', table: 'T-1', source: 'QR Table', layout: 'Mobile',
  capabilities: {
    product_detail_enabled: false, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: false, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true,
    feedback_enabled: false,
  },
}

type Row = MenuItem & { menu_order?: number | null; course_priority?: number | null }
const row = (item: string, course: string | null, menu_order: number, course_priority: number | null): Row => ({
  item, item_name: item, rate: 1000, special_dish: 0, disabled: 0, item_image: null,
  course, course_label: course, menu_order, course_priority,
})

// The API sorts items by name. Priorities: Starters 1, Mains 2; Coffee and
// Beers have none, so they follow in menu-row order (Coffee row 4, Beers row 6).
const menu: Row[] = [
  row('Beer', 'Beers', 6, null),
  row('Burger', 'Mains', 2, 2),
  row('Espresso', 'Coffee', 4, null),
  row('Loose item', null, 9, null),
  row('Samosa', 'Starters', 5, 1),
  row('Steak', 'Mains', 1, 2),
]

// Loaded per test, so a missing helper fails each assertion instead of the file.
async function menuCategories(items: Row[] | MenuItem[]) {
  const specifier = './lib/menu-categories'
  const helper = await import(/* @vite-ignore */ specifier).catch((error: Error) => {
    throw new Error(`lib/menu-categories missing: ${error.message}`)
  })
  return helper.menuCategories(items)
}

describe('menuCategories', () => {
  it('orders courses by serving priority, then by first menu row; items keep menu-row order', async () => {
    const groups = await menuCategories(menu)
    expect(groups.map((g: { course: string | null }) => g.course)).toEqual(['Starters', 'Mains', 'Coffee', 'Beers', null])
    expect(groups.find((g: { course: string | null }) => g.course === 'Mains')!.items.map((i: MenuItem) => i.item)).toEqual(['Steak', 'Burger'])
  })

  it('treats a missing priority and menu order as unconfigured and keeps the API order', async () => {
    const plain = menu.map(({ menu_order: _m, course_priority: _p, ...rest }) => rest)
    expect((await menuCategories(plain)).map((g: { course: string | null }) => g.course)).toEqual(['Beers', 'Mains', 'Coffee', 'Starters', null])
  })

  it('returns no groups for an empty menu', async () => {
    expect(await menuCategories([])).toEqual([])
  })
})

let root: Root
let host: HTMLDivElement

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  sessionStorage.clear()
  localStorage.clear()
  window.history.replaceState(null, '', '/order')
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw new Error(`Unexpected GET ${path}`) })
  vi.spyOn(call, 'post').mockImplementation(async (path: string) => {
    if (path.endsWith('get_customer_menu')) return { message: { items: menu, name: 'Test menu', modified_time: null } }
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

const labels = {
  en: { nav: 'Menu categories', all: 'All Items', other: 'Other' },
  sw: { nav: 'Makundi ya menyu', all: 'Vitu vyote', other: 'Nyingine' },
  lg: { nav: 'Ebika bya menyu', all: 'Ebintu byonna', other: 'Ebirala' },
}

function nav(label: string): HTMLElement {
  const found = host.querySelector<HTMLElement>(`nav[aria-label="${label}"]`)
  expect(found, `category navigation "${label}"`).not.toBeNull()
  return found!
}
function chips(label: string): string[] {
  return [...nav(label).querySelectorAll('button')].map(b => b.textContent?.trim() ?? '')
}
function dishes(): string[] {
  return [...host.querySelectorAll<HTMLElement>('[data-menu-item]')].map(el => el.dataset.menuItem ?? '')
}
async function choose(label: string, text: string) {
  const button = [...nav(label).querySelectorAll('button')].find(b => b.textContent?.trim() === text)
  expect(button, `category chip ${text}`).toBeDefined()
  await act(async () => button!.click())
}

for (const [name, Layout] of Object.entries({ Mobile, Tablet, Landscape, Portrait })) {
  for (const lang of ['en', 'sw', 'lg'] as const) {
    const l = labels[lang]
    describe(`${name}/${lang} guest menu by category`, () => {
      beforeEach(async () => { await initI18n(lang, { setCookie() {} }) })

      it('shows categories in configured order, with All first and uncategorised last', async () => {
        await act(async () => { root.render(<Layout initialContext={context} />) })
        expect(chips(l.nav)).toEqual([l.all, 'Starters', 'Mains', 'Coffee', 'Beers', l.other])
      })

      it('All shows every dish grouped under its category, in menu order', async () => {
        await act(async () => { root.render(<Layout initialContext={context} />) })
        expect(dishes()).toEqual(['Samosa', 'Steak', 'Burger', 'Espresso', 'Beer', 'Loose item'])
        const headings = [...host.querySelectorAll('h2')].map(h => h.textContent?.trim())
        expect(headings).toEqual(expect.arrayContaining(['Starters', 'Mains', 'Coffee', 'Beers', l.other]))
      })

      it('a category chip shows only that category and All restores the full menu', async () => {
        await act(async () => { root.render(<Layout initialContext={context} />) })
        await choose(l.nav, 'Mains')
        expect(dishes()).toEqual(['Steak', 'Burger'])
        await choose(l.nav, l.other)
        expect(dishes()).toEqual(['Loose item'])
        await choose(l.nav, l.all)
        expect(dishes()).toHaveLength(6)
      })
    })
  }
}
