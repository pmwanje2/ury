import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// Ported from the fork's node:test suite (self-order/src/i18n/guest-language.test.mjs,
// 265613bb..b79a2c29) onto the v16 vitest harness. The source pins follow the v16
// layout: shared screens (CartPage, CheckoutScreen, OrderStatusScreen, ProductCard,
// SearchBar) carry guest copy that the fork's layouts had inline.
const here = dirname(fileURLToPath(import.meta.url))
const src = join(here, '..')

const en = {
  "language": {
    "chooser": "Language"
  },
  "assistance": {
    "call": "Call waiter",
    "calling": "Calling waiter…",
    "requested": "Waiter requested — staff notified",
    "already": "Waiter already requested — staff notified"
  },
  "footer": {
    "built_on": "built on",
    "source": "Source"
  },
  "dialog": {
    "close": "Close"
  },
  "header": {
    "order": "Order",
    "pickup": "Order for Pickup",
    "table": "Table {{table}}",
    "location": "{{table}}",
    "area_location": "{{area}} · {{table}}"
  },
  "actions": {
    "start_over": "Start Over",
    "new_order": "New Order"
  },
  "confirm": {
    "start_over": "Start over? Your current cart will be cleared.",
    "new_order": "Start a new order? Current cart will be cleared."
  },
  "status": {
    "loading_menu": "Loading menu…",
    "starting": "Starting up…"
  },
  "order": {
    "so_far": "Your order so far",
    "pickup_code": "Pickup code: {{code}}",
    "total": "Total",
    "place": "Place Order",
    "placing": "Placing order…",
    "placing_short": "Placing…"
  },
  "payment": {
    "pay_online": "Pay Online",
    "starting": "Starting payment…",
    "request_created": "Payment request created ({{amount}} {{currency}}) — a staff member will assist with payment."
  },
  "bill": {
    "request": "Request Bill",
    "requested": "Bill requested — staff notified"
  },
  "cart": {
    "title": "Cart",
    "empty": "Tap a menu item to add it to your cart.",
    "in_cart": "In cart: {{qty}}",
    "remove_one": "Remove one {{name}}",
    "add_one": "Add one more {{name}}",
    "count_one": "{{count}} item",
    "count_many": "{{count}} items",
    "still_there": "Still there?",
    "still_here": "I'm still here"
  },
  "menu": {
    "all_items": "All Items",
    "other": "Other",
    "categories": "Menu categories"
  },
  "errors": {
    "missing_code": "This link is missing an ordering code. Please rescan the QR code on your table.",
    "load_menu": "Unable to load the menu. Please rescan the QR code.",
    "place_order": "Could not place the order. Please try again.",
    "request_bill": "Could not request the bill. Please ask staff for help.",
    "pay_online": "Could not start online payment. Please pay at the counter.",
    "call_waiter": "Could not call a waiter. Please ask staff for help.",
    "device_start": "This device could not be started. Please contact staff.",
    "device_bootstrap": "Unable to bootstrap this device."
  }
}

const sw = {
  "language": {
    "chooser": "Lugha"
  },
  "assistance": {
    "call": "Ita mhudumu",
    "calling": "Inaita mhudumu…",
    "requested": "Mhudumu ameombwa — wahudumu wamearifiwa",
    "already": "Mhudumu tayari ameombwa — wahudumu wamearifiwa"
  },
  "footer": {
    "built_on": "imejengwa kwa",
    "source": "Chanzo"
  },
  "dialog": {
    "close": "Funga"
  },
  "header": {
    "order": "Oda",
    "pickup": "Oda ya kuchukua",
    "table": "Meza {{table}}",
    "location": "{{table}}",
    "area_location": "{{area}} · {{table}}"
  },
  "actions": {
    "start_over": "Anza upya",
    "new_order": "Oda mpya"
  },
  "confirm": {
    "start_over": "Anza upya? Kikapu chako kitafutwa.",
    "new_order": "Anza oda mpya? Kikapu cha sasa kitafutwa."
  },
  "status": {
    "loading_menu": "Inapakia menyu…",
    "starting": "Inaanzisha…"
  },
  "order": {
    "so_far": "Oda yako hadi sasa",
    "pickup_code": "Msimbo wa kuchukua: {{code}}",
    "total": "Jumla",
    "place": "Weka oda",
    "placing": "Inaweka oda…",
    "placing_short": "Inaweka…"
  },
  "payment": {
    "pay_online": "Lipa mtandaoni",
    "starting": "Inaanzisha malipo…",
    "request_created": "Ombi la malipo limeundwa ({{amount}} {{currency}}) — mhudumu atakusaidia kulipa."
  },
  "bill": {
    "request": "Omba bili",
    "requested": "Bili imeombwa — wahudumu wamearifiwa"
  },
  "cart": {
    "title": "Kikapu",
    "empty": "Gusa chakula kwenye menyu ili uweke kikapuni.",
    "in_cart": "Kikapuni: {{qty}}",
    "remove_one": "Ondoa moja {{name}}",
    "add_one": "Ongeza moja {{name}}",
    "count_one": "Bidhaa {{count}}",
    "count_many": "Bidhaa {{count}}",
    "still_there": "Bado uko?",
    "still_here": "Bado niko"
  },
  "menu": {
    "all_items": "Vitu vyote",
    "other": "Nyingine",
    "categories": "Makundi ya menyu"
  },
  "errors": {
    "missing_code": "Kiungo hiki hakina msimbo wa oda. Tafadhali skani tena msimbo wa QR ulio mezani.",
    "load_menu": "Imeshindwa kupakia menyu. Tafadhali skani tena msimbo wa QR.",
    "place_order": "Imeshindwa kuweka oda. Tafadhali jaribu tena.",
    "request_bill": "Imeshindwa kuomba bili. Tafadhali muombe mhudumu msaada.",
    "pay_online": "Imeshindwa kuanzisha malipo mtandaoni. Tafadhali lipa kwenye kaunta.",
    "call_waiter": "Imeshindwa kuita mhudumu. Tafadhali waombe wahudumu msaada.",
    "device_start": "Kifaa hiki hakikuweza kuanzishwa. Tafadhali wasiliana na wahudumu.",
    "device_bootstrap": "Imeshindwa kuanzisha kifaa hiki."
  }
}

const lg = {
  "language": {
    "chooser": "Olulimi"
  },
  "assistance": {
    "call": "Yita omuweereza",
    "calling": "Nyita omuweereza…",
    "requested": "Omuweereza asabiddwa — abaweereza bategeezeddwa",
    "already": "Omuweereza yali asabiddwa — abaweereza bategeezeddwa"
  },
  "footer": {
    "built_on": "ezimbiddwa ku",
    "source": "Ensibuko"
  },
  "dialog": {
    "close": "Ggalawo"
  },
  "header": {
    "order": "Oda",
    "pickup": "Oda ey'okutwala",
    "table": "Emmeeza {{table}}",
    "location": "{{table}}",
    "area_location": "{{area}} · {{table}}"
  },
  "actions": {
    "start_over": "Tandika buto",
    "new_order": "Oda empya"
  },
  "confirm": {
    "start_over": "Tandika buto? Ebintu byonna ebiri mu kikapu kyo bijja kuggyibwamu.",
    "new_order": "Tandika oda empya? Ebintu byonna ebiri mu kikapu ekiriwo bijja kuggyibwamu."
  },
  "status": {
    "loading_menu": "Menyu ejja…",
    "starting": "Kitandika…"
  },
  "order": {
    "so_far": "Oda yo okutuusa kati",
    "pickup_code": "Koodi y'okutwala: {{code}}",
    "total": "Omugatte",
    "place": "Teeka oda",
    "placing": "Nteeka oda…",
    "placing_short": "Nteeka…"
  },
  "payment": {
    "pay_online": "Sasula ku yintaneeti",
    "starting": "Ntandika okusasula…",
    "request_created": "Okusaba okusasula kuteekeddwa ({{amount}} {{currency}}) — omuweereza ajja kukuyamba okusasula."
  },
  "bill": {
    "request": "Saba bili",
    "requested": "Bili esabiddwa — abaweereza bategeezeddwa"
  },
  "cart": {
    "title": "Ekikapu",
    "empty": "Nyiga ku mmere ku menyu ogiteeke mu kikapu.",
    "in_cart": "Mu kikapu: {{qty}}",
    "remove_one": "Gyawo emu {{name}}",
    "add_one": "Yongerako emu {{name}}",
    "count_one": "Ekintu {{count}}",
    "count_many": "Ebintu {{count}}",
    "still_there": "Okyali wano?",
    "still_here": "Nkyali wano"
  },
  "menu": {
    "all_items": "Ebintu byonna",
    "other": "Ebirala",
    "categories": "Ebika bya menyu"
  },
  "errors": {
    "missing_code": "Link eno terina koodi y'oda. Nsaba oskane nate koodi ya QR eri ku mmeeza.",
    "load_menu": "Menyu tetuuse. Nsaba oskane nate koodi ya QR.",
    "place_order": "Oda tetuuse kuteekebwa. Nsaba ogezeeko nate.",
    "request_bill": "Bili tesabiddwa. Nsaba obuuze omuweereza.",
    "pay_online": "Okusasula ku yintaneeti tekutandise. Nsaba osasule ku kaunta.",
    "call_waiter": "Omuweereza tayitiddwa. Nsaba obuuze abaweereza.",
    "device_start": "Ekyuma kino tekinatandika. Nsaba owulize abaweereza.",
    "device_bootstrap": "Ekyuma kino tekinatandika."
  }
}

type Tree = { [key: string]: string | Tree }

function leafPaths(value: string | Tree, prefix = ''): string[] {
  if (typeof value === 'string') return [prefix]
  return Object.entries(value).flatMap(([key, child]) => leafPaths(child, prefix ? `${prefix}.${key}` : key))
}

function tokens(text: string): string[] {
  return [...text.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]).sort()
}

function readSource(rel: string): string {
  try {
    return readFileSync(join(src, rel), 'utf8')
  } catch (error) {
    throw new Error(`${rel} missing: ${(error as NodeJS.ErrnoException).code ?? error}`)
  }
}

function readLocale(code: string): Tree {
  try {
    return JSON.parse(readFileSync(join(here, 'locales', `${code}.json`), 'utf8'))
  } catch (error) {
    throw new Error(`${code}.json missing: ${(error as NodeJS.ErrnoException).code ?? error}`)
  }
}

// Loaded per test, so a missing module fails each assertion instead of the file.
async function loadModule(specifier: string) {
  return import(/* @vite-ignore */ specifier).catch((error: Error) => {
    throw new Error(`${specifier} missing: ${error.message}`)
  })
}

// Locale-neutral location labels (#97): the area and location are ERP names, never translated.
const LOCATION_KEYS = new Set(['header.location', 'header.area_location'])

const HARDCODED_GUEST_COPY = [
  'Place Order',
  'Request Bill',
  'Loading menu',
  'Pay Online',
  'Your order so far',
  'In cart:',
  'Start Over',
  'New Order',
  'Order for Pickup',
  'Still there?',
  "I'm still here",
  'All Items',
  'Placing order',
  'Starting payment',
  'Bill requested',
  'Start over?',
  'Start a new order?',
  'Tap a menu item',
  'Please rescan the QR code',
  'Could not place the order',
  'Could not request the bill',
  'Could not start online payment',
  'Starting up',
  'This device could not be started',
  'Unable to bootstrap this device',
  'Menu categories',
  '>Total<',
  '>Cart<',
  "?? 'Other'",
  "'Uncategorized'",
  '`Table ${',
]

const GUEST_UI = [
  'layouts/MobileQRLayout.tsx',
  'layouts/shared/CategoryTabs.tsx',
  'layouts/TabletLayout.tsx',
  'layouts/LandscapeKioskLayout.tsx',
  'layouts/PortraitKioskLayout.tsx',
  'layouts/shared/CartPanel.tsx',
  'layouts/shared/CheckoutScreen.tsx',
  'layouts/shared/MenuGrid.tsx',
  'layouts/shared/ProductCard.tsx',
  'App.tsx',
  'hooks/useOrderingSession.ts',
  'hooks/useDeviceBootstrap.ts',
]

describe('guest ordering languages', () => {
  it('browser language is the default and an explicit choice wins', async () => {
    const resolver = await loadModule('./resolve-language')
    expect(resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['lg-UG', 'en-US'], bootLang: 'en' })).toBe('lg')
    expect(resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['sw_KE'], bootLang: 'en' })).toBe('sw')
    expect(resolver.resolveGuestLanguage({ stored: 'SW', browserLanguages: ['en-GB'], bootLang: 'lg' })).toBe('sw')
    expect(resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['en-GB'], bootLang: 'sw' })).toBe('en')
    expect(resolver.resolveGuestLanguage({ stored: 'fr', browserLanguages: ['fr-FR', 'de'], bootLang: 'sw' })).toBe('sw')
    expect(resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['fr-FR'], bootLang: null })).toBe('en')
    expect(resolver.GUEST_LANGUAGES.map((language: { code: string; label: string }) => [language.code, language.label]))
      .toEqual([['en', 'EN'], ['sw', 'Kiswahili'], ['lg', 'Luganda']])
  })

  it('switcher choice is stored and mirrored to the Frappe guest cookie', async () => {
    const resolver = await loadModule('./resolve-language')
    const stored = new Map<string, string>()
    const cookies: string[] = []
    const selected = resolver.selectGuestLanguage('lg', {
      storage: { setItem: (key: string, value: string) => stored.set(key, value) },
      setCookie: (value: string) => cookies.push(value),
    })
    expect(selected).toBe('lg')
    expect(stored.get(resolver.GUEST_LANGUAGE_STORAGE_KEY)).toBe('lg')
    expect(cookies[0]).toBe('preferred_language=lg; Path=/; Max-Age=31536000; SameSite=Lax')
    expect(() => resolver.selectGuestLanguage('ar', { storage: { setItem() {} }, setCookie() {} })).toThrow()
  })

  it('sw and lg catalogs cover every guest string and keep dish names out', async () => {
    const translate = await loadModule('./translate')
    const catalogs: Record<string, Tree> = { en: readLocale('en'), sw: readLocale('sw'), lg: readLocale('lg') }
    expect(catalogs.en).toEqual(en)
    expect(catalogs.sw).toEqual(sw)
    expect(catalogs.lg).toEqual(lg)
    const paths = leafPaths(en)
    for (const code of ['sw', 'lg']) {
      expect(leafPaths(catalogs[code])).toEqual(paths)
      for (const path of paths) {
        const english = translate.translate(en, path)
        const local = translate.translate(catalogs[code], path)
        if (!LOCATION_KEYS.has(path)) expect(local, `${code} ${path} is still English`).not.toBe(english)
        expect(tokens(local), `${code} ${path} dropped a placeholder`).toEqual(tokens(english))
      }
    }
    const serialized = JSON.stringify(catalogs)
    expect(serialized.includes('Tilapia') || serialized.includes('Rolex')).toBe(false)
    expect(translate.translate(catalogs.sw, 'header.table', { table: 'T-12' })).toBe('Meza T-12')
    expect(translate.translate(catalogs.lg, 'cart.count_many', { count: 2 })).toBe('Ebintu 2')
    expect(translate.translate({}, 'order.total', undefined, catalogs.en)).toBe('Total')
    expect(translate.translate({}, 'missing.key', undefined, catalogs.en)).toBe('missing.key')
  })

  it('initI18n activates the catalog before the page renders', async () => {
    const i18n = await loadModule('./index')
    const cookies: string[] = []
    const root = { lang: '', dir: '' }
    await i18n.initI18n('sw', { setCookie: (value: string) => cookies.push(value) })
    expect(i18n.getActiveLanguage()).toBe('sw')
    expect(i18n.t('order.place')).toBe('Weka oda')
    expect(i18n.t('bill.request')).toBe('Omba bili')
    expect(cookies[0]).toBe('preferred_language=sw; Path=/; Max-Age=31536000; SameSite=Lax')
    await i18n.initI18n('lg', { setCookie() {} })
    i18n.applyDocumentLocale({ documentElement: root })
    expect(root.lang).toBe('lg')
    expect(root.dir).toBe('ltr')
    expect(i18n.t('order.place')).toBe('Teeka oda')
    expect(i18n.t('not.a.real.key')).toBe('not.a.real.key')
    await i18n.initI18n('en', { setCookie() {} })
  })

  it('guest surfaces wrap copy and keep ERP dish names', () => {
    const offenders: string[] = []
    for (const rel of GUEST_UI) {
      const text = readSource(rel)
      for (const phrase of HARDCODED_GUEST_COPY) {
        if (text.includes(phrase)) offenders.push(`${rel}: ${phrase}`)
      }
    }
    expect(offenders).toEqual([])

    const mobile = readSource('layouts/MobileQRLayout.tsx')
    for (const needle of [
      'LanguageSwitcher',
      "t('bill.request')",
      'locationLabel(context.table, context.area)',
      "t('header.pickup')",
      'row.item_name',
    ]) {
      expect(mobile.includes(needle), `MobileQRLayout missing ${needle}`).toBe(true)
    }

    for (const rel of ['layouts/TabletLayout.tsx', 'layouts/LandscapeKioskLayout.tsx', 'layouts/PortraitKioskLayout.tsx']) {
      const text = readSource(rel)
      expect(text.includes('LanguageSwitcher'), rel).toBe(true)
      expect(text.includes("t('cart.still_there')"), rel).toBe(true)
      expect(text.includes("t('actions.new_order')"), rel).toBe(true)
      expect(text.includes('locationLabel(context.table, context.area)'), rel).toBe(true)
    }

    const portrait = readSource('layouts/PortraitKioskLayout.tsx')
    expect(portrait.includes("t('menu.all_items')")).toBe(true)
    expect(portrait.includes("t('menu.other')")).toBe(true)

    const cart = readSource('layouts/shared/CartPanel.tsx')
    expect(cart.includes("t('cart.title')")).toBe(true)
    expect(cart.includes("t('cart.empty')")).toBe(true)
    expect(cart.includes('item.item_name')).toBe(true)

    const card = readSource('layouts/shared/ProductCard.tsx')
    expect(card.includes("t('cart.in_cart'")).toBe(true)
    expect(card.includes('item.item_name')).toBe(true)

    const app = readSource('App.tsx')
    expect(app.includes("t('status.starting')")).toBe(true)
    expect(app.includes("t('errors.device_start')")).toBe(true)

    const session = readSource('hooks/useOrderingSession.ts')
    for (const key of ['missing_code', 'load_menu', 'place_order', 'request_bill', 'pay_online']) {
      expect(session.includes(`t('errors.${key}')`), key).toBe(true)
    }

    expect(readSource('hooks/useDeviceBootstrap.ts').includes("t('errors.device_bootstrap')")).toBe(true)

    const main = readSource('main.tsx')
    expect(main.includes('initI18n')).toBe(true)
    expect(main.includes('applyDocumentLocale')).toBe(true)
    expect(main.indexOf('initI18n') >= 0 && main.indexOf('createRoot') > main.indexOf('initI18n')).toBe(true)

    const switcher = readSource('components/LanguageSwitcher.tsx')
    expect(switcher.includes('selectGuestLanguage')).toBe(true)
    expect(switcher.includes('window.location.reload')).toBe(false)
    expect(switcher).toMatch(/minHeight:\s*44/)
    expect(switcher).toMatch(/minWidth:\s*44/)
    expect(switcher.includes('role="group"')).toBe(true)
    expect(switcher.includes('GUEST_LANGUAGES')).toBe(true)

    const loader = readSource('i18n/loader.ts')
    expect(loader.includes("from './locales/en.json'")).toBe(true)
    expect(loader.includes("from './locales/sw.json'")).toBe(true)
    expect(loader.includes("from './locales/lg.json'")).toBe(true)
    expect(loader.includes('locales/${lang}.json')).toBe(false)

    // The staff-only tablet assignment screen stays English.
    expect(readSource('layouts/PortableTabletAssignment.tsx').includes('Assign This Tablet')).toBe(true)
  })
})

describe('guest wording, waiter copy, and footer follow the catalog', () => {
  it('swahili uses the locative and luganda names every cart item', () => {
    const swFile = readLocale('sw') as Record<string, Record<string, string>>
    const lgFile = readLocale('lg') as Record<string, Record<string, string>>
    expect(swFile.cart?.empty).toBe('Gusa chakula kwenye menyu ili uweke kikapuni.')
    expect(swFile.errors?.pay_online).toBe('Imeshindwa kuanzisha malipo mtandaoni. Tafadhali lipa kwenye kaunta.')
    expect(lgFile.bill?.requested).toBe('Bili esabiddwa — abaweereza bategeezeddwa')
    expect(lgFile.errors?.device_start).toBe('Ekyuma kino tekinatandika. Nsaba owulize abaweereza.')
    expect(lgFile.confirm?.start_over).toBe('Tandika buto? Ebintu byonna ebiri mu kikapu kyo bijja kuggyibwamu.')
    expect(lgFile.confirm?.new_order).toBe('Tandika oda empya? Ebintu byonna ebiri mu kikapu ekiriwo bijja kuggyibwamu.')
  })

  it('call waiter, both confirmations, and the refusal fallback are translated', () => {
    const mobile = readSource('layouts/MobileQRLayout.tsx')
    const session = readSource('hooks/useOrderingSession.ts')
    const surface = `${mobile}\n${session}`
    for (const phrase of ['Call waiter', 'Calling waiter', 'Waiter requested', 'Waiter already requested', 'Could not call a waiter']) {
      expect(surface.includes(phrase), phrase).toBe(false)
    }
    for (const key of ["t('assistance.call')", "t('assistance.calling')", "t('assistance.requested')", "t('assistance.already')", "t('errors.call_waiter')"]) {
      expect(surface.includes(key), key).toBe(true)
    }
  })

  it('the visible footer words follow the guest language', async () => {
    const i18n = await loadModule('./index')
    expect(typeof i18n.applyStaticGuestCopy).toBe('function')
    const nodes = [
      { key: 'footer.built_on', textContent: 'built on', getAttribute: (_: string) => 'footer.built_on' as string | null },
      { key: 'footer.source', textContent: 'Source', getAttribute: (_: string) => 'footer.source' as string | null },
    ]
    const root = { querySelectorAll: () => nodes }
    await i18n.initI18n('sw', { setCookie() {} })
    i18n.applyStaticGuestCopy(root)
    expect(nodes[0].textContent).toBe('imejengwa kwa')
    expect(nodes[1].textContent).toBe('Chanzo')
    await i18n.initI18n('lg', { setCookie() {} })
    i18n.applyStaticGuestCopy(root)
    expect(nodes[0].textContent).toBe('ezimbiddwa ku')
    expect(nodes[1].textContent).toBe('Ensibuko')
    await i18n.initI18n('en', { setCookie() {} })
  })

  it('a recovery error and a waiter confirmation are translated when shown', () => {
    const session = readSource('hooks/useOrderingSession.ts')
    expect(session.includes('setError(t('), 'recovery error is frozen with t() when it is stored').toBe(false)
    expect(session.includes('setAssistanceMessage('), 'waiter confirmation is frozen with t() when it is stored').toBe(false)
    for (const key of ['missing_code', 'load_menu', 'place_order', 'request_bill', 'call_waiter', 'pay_online']) {
      expect(session.includes(`t('errors.${key}')`), key).toBe(true)
    }
    expect(session.includes("t('assistance.requested')")).toBe(true)
    expect(session.includes("t('assistance.already')")).toBe(true)
  })

  it('switching language rewrites the static copy already on the page', () => {
    const switcher = readSource('components/LanguageSwitcher.tsx')
    const handlerStart = switcher.indexOf('onClick={() => {')
    expect(handlerStart >= 0).toBe(true)
    const handler = switcher.slice(handlerStart, switcher.indexOf('</button>', handlerStart))
    expect(handler.includes('initI18n(language.code)')).toBe(true)
    expect(handler.includes('applyDocumentLocale()')).toBe(true)
    expect(handler.includes('applyStaticGuestCopy()'), 'language switch leaves static copy in the previous language').toBe(true)
  })

  it('a device recovery error is translated when it is shown', () => {
    const source = readSource('hooks/useDeviceBootstrap.ts')
    expect(source.includes("setDeviceError(err instanceof Error ? err.message : t('errors.device_bootstrap'))"),
      'device recovery error is frozen with t() when it is stored').toBe(false)
    expect(source.includes("t('errors.device_bootstrap')")).toBe(true)
  })

  it('a category label is not saved already translated', () => {
    // #98: category grouping is shared by every guest layout and memoised, so
    // it keeps the uncategorised label empty and each render supplies "Other".
    const grouping = readSource('lib/menu-categories.ts')
    expect(/\bt\(|i18n/.test(grouping), 'menu grouping translates inside a memoised value').toBe(false)
    for (const rel of [
      'layouts/MobileQRLayout.tsx',
      'layouts/TabletLayout.tsx',
      'layouts/LandscapeKioskLayout.tsx',
      'layouts/PortraitKioskLayout.tsx',
    ]) {
      const source = readSource(rel)
      const memoStart = source.indexOf('useMemo(() => menuCategories(')
      expect(memoStart >= 0, `${rel} groups the menu with menuCategories`).toBe(true)
      const memoEnd = source.indexOf(')', source.indexOf('[menu]', memoStart))
      expect(/\bt\(/.test(source.slice(memoStart, memoEnd)), `${rel} freezes a category label inside useMemo`).toBe(false)
      expect(source.includes("t('menu.other')"), rel).toBe(true)
    }
  })

  it('a missing ordering code and a device error still offer the language switcher', () => {
    for (const rel of [
      'layouts/MobileQRLayout.tsx',
      'layouts/TabletLayout.tsx',
      'layouts/LandscapeKioskLayout.tsx',
      'layouts/PortraitKioskLayout.tsx',
    ]) {
      const source = readSource(rel)
      const start = source.indexOf('if (loading)')
      const end = source.indexOf('\n  // ', source.indexOf('if (error && !context)', start))
      expect(start >= 0 && end > start, rel).toBe(true)
      expect(source.slice(start, end).includes('<LanguageSwitcher />'), rel).toBe(true)
    }
    const app = readSource('App.tsx')
    const start = app.indexOf('if (deviceLoading)')
    const end = app.indexOf('const Layout', start)
    expect(app.slice(start, end).includes('<LanguageSwitcher />')).toBe(true)
  })
})
