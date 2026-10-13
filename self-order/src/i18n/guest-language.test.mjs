import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = join(here, '..')

const en = {
  language: { chooser: 'Language' },
  header: {
    order: 'Order',
    pickup: 'Order for Pickup',
    table: 'Table {{table}}',
  },
  actions: {
    start_over: 'Start Over',
    new_order: 'New Order',
  },
  confirm: {
    start_over: 'Start over? Your current cart will be cleared.',
    new_order: 'Start a new order? Current cart will be cleared.',
  },
  status: {
    loading_menu: 'Loading menu…',
    starting: 'Starting up…',
  },
  order: {
    so_far: 'Your order so far',
    pickup_code: 'Pickup code: {{code}}',
    total: 'Total',
    place: 'Place Order',
    placing: 'Placing order…',
    placing_short: 'Placing…',
  },
  payment: {
    pay_online: 'Pay Online',
    starting: 'Starting payment…',
    request_created:
      'Payment request created ({{amount}} {{currency}}) — a staff member will assist with payment.',
  },
  bill: {
    request: 'Request Bill',
    requested: 'Bill requested — staff notified',
  },
  cart: {
    title: 'Cart',
    empty: 'Tap a menu item to add it to your cart.',
    in_cart: 'In cart: {{qty}}',
    remove_one: 'Remove one {{name}}',
    add_one: 'Add one more {{name}}',
    count_one: '{{count}} item',
    count_many: '{{count}} items',
    still_there: 'Still there?',
    still_here: "I'm still here",
  },
  menu: {
    all_items: 'All Items',
    other: 'Other',
    categories: 'Menu categories',
  },
  errors: {
    missing_code: 'This link is missing an ordering code. Please rescan the QR code on your table.',
    load_menu: 'Unable to load the menu. Please rescan the QR code.',
    place_order: 'Could not place the order. Please try again.',
    request_bill: 'Could not request the bill. Please ask staff for help.',
    pay_online: 'Could not start online payment. Please pay at the counter.',
    device_start: 'This device could not be started. Please contact staff.',
    device_bootstrap: 'Unable to bootstrap this device.',
  },
}

const sw = {
  language: { chooser: 'Lugha' },
  header: {
    order: 'Oda',
    pickup: 'Oda ya kuchukua',
    table: 'Meza {{table}}',
  },
  actions: {
    start_over: 'Anza upya',
    new_order: 'Oda mpya',
  },
  confirm: {
    start_over: 'Anza upya? Kikapu chako kitafutwa.',
    new_order: 'Anza oda mpya? Kikapu cha sasa kitafutwa.',
  },
  status: {
    loading_menu: 'Inapakia menyu…',
    starting: 'Inaanzisha…',
  },
  order: {
    so_far: 'Oda yako hadi sasa',
    pickup_code: 'Msimbo wa kuchukua: {{code}}',
    total: 'Jumla',
    place: 'Weka oda',
    placing: 'Inaweka oda…',
    placing_short: 'Inaweka…',
  },
  payment: {
    pay_online: 'Lipa mtandaoni',
    starting: 'Inaanzisha malipo…',
    request_created:
      'Ombi la malipo limeundwa ({{amount}} {{currency}}) — mhudumu atakusaidia kulipa.',
  },
  bill: {
    request: 'Omba bili',
    requested: 'Bili imeombwa — wahudumu wamearifiwa',
  },
  cart: {
    title: 'Kikapu',
    empty: 'Gusa chakula ku menyu ili uweke kikapuni.',
    in_cart: 'Kikapuni: {{qty}}',
    remove_one: 'Ondoa moja {{name}}',
    add_one: 'Ongeza moja {{name}}',
    count_one: 'Bidhaa {{count}}',
    count_many: 'Bidhaa {{count}}',
    still_there: 'Bado uko?',
    still_here: 'Bado niko',
  },
  menu: {
    all_items: 'Vitu vyote',
    other: 'Nyingine',
    categories: 'Makundi ya menyu',
  },
  errors: {
    missing_code: 'Kiungo hiki hakina msimbo wa oda. Tafadhali skani tena msimbo wa QR ulio mezani.',
    load_menu: 'Imeshindwa kupakia menyu. Tafadhali skani tena msimbo wa QR.',
    place_order: 'Imeshindwa kuweka oda. Tafadhali jaribu tena.',
    request_bill: 'Imeshindwa kuomba bili. Tafadhali muombe mhudumu msaada.',
    pay_online: 'Imeshindwa kuanzisha malipo mtandaoni. Tafadhali lipa kaunta.',
    device_start: 'Kifaa hiki hakikuweza kuanzishwa. Tafadhali wasiliana na wahudumu.',
    device_bootstrap: 'Imeshindwa kuanzisha kifaa hiki.',
  },
}

const lg = {
  language: { chooser: 'Olulimi' },
  header: {
    order: 'Oda',
    pickup: "Oda ey'okutwala",
    table: 'Emmeeza {{table}}',
  },
  actions: {
    start_over: 'Tandika buto',
    new_order: 'Oda empya',
  },
  confirm: {
    start_over: 'Tandika buto? Ekikapu kyo kinaaggyibwamu.',
    new_order: 'Tandika oda empya? Ekikapu ekiriwo kinaaggyibwamu.',
  },
  status: {
    loading_menu: 'Menyu ejja…',
    starting: 'Kitandika…',
  },
  order: {
    so_far: 'Oda yo okutuusa kati',
    pickup_code: "Koodi y'okutwala: {{code}}",
    total: 'Omugatte',
    place: 'Teeka oda',
    placing: 'Nteeka oda…',
    placing_short: 'Nteeka…',
  },
  payment: {
    pay_online: 'Sasula ku yintaneeti',
    starting: 'Ntandika okusasula…',
    request_created:
      'Okusaba okusasula kuteekeddwa ({{amount}} {{currency}}) — omuweereza ajja kukuyamba okusasula.',
  },
  bill: {
    request: 'Saba bili',
    requested: 'Bili esabiddwa — abawereza bategeezeddwa',
  },
  cart: {
    title: 'Ekikapu',
    empty: 'Nyiga ku mmere ku menyu ogiteeke mu kikapu.',
    in_cart: 'Mu kikapu: {{qty}}',
    remove_one: 'Gyawo emu {{name}}',
    add_one: 'Yongerako emu {{name}}',
    count_one: 'Ekintu {{count}}',
    count_many: 'Ebintu {{count}}',
    still_there: 'Okyali wano?',
    still_here: 'Nkyali wano',
  },
  menu: {
    all_items: 'Ebintu byonna',
    other: 'Ebirala',
    categories: 'Ebika bya menyu',
  },
  errors: {
    missing_code: 'Link eno terina koodi y\'oda. Nsaba oskane nate koodi ya QR eri ku mmeeza.',
    load_menu: 'Menyu tetuuse. Nsaba oskane nate koodi ya QR.',
    place_order: 'Oda tetuuse kuteekebwa. Nsaba ogezeeko nate.',
    request_bill: 'Bili tesabiddwa. Nsaba obuuze omuweereza.',
    pay_online: 'Okusasula ku yintaneeti tekutandise. Nsaba osasule ku kaunta.',
    device_start: 'Ekyuma kino tekinatandika. Nsaba owulize abawereza.',
    device_bootstrap: 'Ekyuma kino tekinatandika.',
  },
}

function leafPaths(value, prefix = '') {
  if (typeof value === 'string') return [prefix]
  return Object.entries(value).flatMap(([key, child]) => leafPaths(child, prefix ? `${prefix}.${key}` : key))
}

function tokens(text) {
  return [...text.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]).sort()
}

function readSource(rel) {
  try {
    return readFileSync(join(src, rel), 'utf8')
  } catch (error) {
    assert.fail(`${rel} missing: ${error.code || error.message}`)
  }
}

function readLocale(code) {
  try {
    return JSON.parse(readFileSync(join(here, 'locales', `${code}.json`), 'utf8'))
  } catch (error) {
    assert.fail(`${code}.json missing: ${error.code || error.message}`)
  }
}

async function loadModule(specifier) {
  try {
    return await import(specifier)
  } catch (error) {
    assert.fail(`${specifier} missing: ${error.code || error.message}`)
  }
}

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
]

const GUEST_UI = [
  'layouts/MobileQRLayout.tsx',
  'layouts/TabletLayout.tsx',
  'layouts/LandscapeKioskLayout.tsx',
  'layouts/PortraitKioskLayout.tsx',
  'layouts/shared/CartPanel.tsx',
  'layouts/shared/MenuGrid.tsx',
  'App.tsx',
  'hooks/useOrderingSession.ts',
  'hooks/useDeviceBootstrap.ts',
]

test('guest ordering languages', async (t) => {
  await t.test('browser language is the default and an explicit choice wins', async () => {
    const resolver = await loadModule('./resolve-language.ts')
    assert.equal(
      resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['lg-UG', 'en-US'], bootLang: 'en' }),
      'lg',
    )
    assert.equal(
      resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['sw_KE'], bootLang: 'en' }),
      'sw',
    )
    assert.equal(
      resolver.resolveGuestLanguage({ stored: 'SW', browserLanguages: ['en-GB'], bootLang: 'lg' }),
      'sw',
    )
    assert.equal(
      resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['en-GB'], bootLang: 'sw' }),
      'en',
    )
    assert.equal(
      resolver.resolveGuestLanguage({ stored: 'fr', browserLanguages: ['fr-FR', 'de'], bootLang: 'sw' }),
      'sw',
    )
    assert.equal(
      resolver.resolveGuestLanguage({ stored: null, browserLanguages: ['fr-FR'], bootLang: null }),
      'en',
    )
    assert.deepEqual(
      resolver.GUEST_LANGUAGES.map((language) => [language.code, language.label]),
      [['en', 'EN'], ['sw', 'Kiswahili'], ['lg', 'Luganda']],
    )
  })

  await t.test('switcher choice is stored and mirrored to the Frappe guest cookie', async () => {
    const resolver = await loadModule('./resolve-language.ts')
    const stored = new Map()
    const cookies = []
    const selected = resolver.selectGuestLanguage('lg', {
      storage: {
        setItem(key, value) {
          stored.set(key, value)
        },
      },
      setCookie(value) {
        cookies.push(value)
      },
    })
    assert.equal(selected, 'lg')
    assert.equal(stored.get(resolver.GUEST_LANGUAGE_STORAGE_KEY), 'lg')
    assert.equal(cookies[0], 'preferred_language=lg; Path=/; Max-Age=31536000; SameSite=Lax')
    assert.throws(() => resolver.selectGuestLanguage('ar', { storage: { setItem() {} }, setCookie() {} }))
  })

  await t.test('sw and lg catalogs cover every guest string and keep dish names out', async () => {
    const translate = await loadModule('./translate.ts')
    const catalogs = { en: readLocale('en'), sw: readLocale('sw'), lg: readLocale('lg') }
    assert.deepEqual(catalogs.en, en)
    assert.deepEqual(catalogs.sw, sw)
    assert.deepEqual(catalogs.lg, lg)
    const paths = leafPaths(en)
    for (const code of ['sw', 'lg']) {
      assert.deepEqual(leafPaths(catalogs[code]), paths)
      for (const path of paths) {
        const english = translate.translate(en, path)
        const local = translate.translate(catalogs[code], path)
        assert.notEqual(local, english, `${code} ${path} is still English`)
        assert.deepEqual(tokens(local), tokens(english), `${code} ${path} dropped a placeholder`)
      }
    }
    const serialized = JSON.stringify(catalogs)
    assert.equal(serialized.includes('Tilapia') || serialized.includes('Rolex'), false)
    assert.equal(translate.translate(catalogs.sw, 'header.table', { table: 'T-12' }), 'Meza T-12')
    assert.equal(translate.translate(catalogs.lg, 'cart.count_many', { count: 2 }), 'Ebintu 2')
    assert.equal(translate.translate({}, 'order.total', undefined, catalogs.en), 'Total')
    assert.equal(translate.translate({}, 'missing.key', undefined, catalogs.en), 'missing.key')
  })

  await t.test('initI18n activates the catalog before the page renders', async () => {
    const i18n = await loadModule('./index.ts')
    const cookies = []
    const root = { lang: '', dir: '' }
    await i18n.initI18n('sw', { setCookie(value) { cookies.push(value) } })
    assert.equal(i18n.getActiveLanguage(), 'sw')
    assert.equal(i18n.t('order.place'), 'Weka oda')
    assert.equal(i18n.t('bill.request'), 'Omba bili')
    assert.equal(cookies[0], 'preferred_language=sw; Path=/; Max-Age=31536000; SameSite=Lax')
    await i18n.initI18n('lg')
    i18n.applyDocumentLocale({ documentElement: root })
    assert.equal(root.lang, 'lg')
    assert.equal(root.dir, 'ltr')
    assert.equal(i18n.t('order.place'), 'Teeka oda')
    assert.equal(i18n.t('not.a.real.key'), 'not.a.real.key')
  })

  await t.test('guest surfaces wrap copy and keep ERP dish names', async () => {
    const offenders = []
    for (const rel of GUEST_UI) {
      const text = readSource(rel)
      for (const phrase of HARDCODED_GUEST_COPY) {
        if (text.includes(phrase)) offenders.push(`${rel}: ${phrase}`)
      }
    }
    assert.deepEqual(offenders, [])

    const mobile = readSource('layouts/MobileQRLayout.tsx')
    for (const needle of [
      'LanguageSwitcher',
      "t('order.place')",
      "t('bill.request')",
      "t('header.table'",
      "t('header.pickup')",
      "t('cart.in_cart'",
      'item.item_name',
      'row.item_name',
      'formatCurrency(',
    ]) {
      assert.equal(mobile.includes(needle), true, `MobileQRLayout missing ${needle}`)
    }

    for (const rel of ['layouts/TabletLayout.tsx', 'layouts/LandscapeKioskLayout.tsx', 'layouts/PortraitKioskLayout.tsx']) {
      const text = readSource(rel)
      assert.equal(text.includes('LanguageSwitcher'), true, rel)
      assert.equal(text.includes("t('cart.still_there')"), true, rel)
      assert.equal(text.includes("t('actions.new_order')"), true, rel)
    }

    const portrait = readSource('layouts/PortraitKioskLayout.tsx')
    assert.equal(portrait.includes("t('menu.all_items')"), true)
    assert.equal(portrait.includes("t('menu.other')"), true)
    assert.equal(portrait.includes("t('order.placing_short')"), true)
    assert.equal(portrait.includes('item.item_name'), true)

    const cart = readSource('layouts/shared/CartPanel.tsx')
    assert.equal(cart.includes("t('cart.title')"), true)
    assert.equal(cart.includes("t('cart.empty')"), true)
    assert.equal(cart.includes('item.item_name'), true)

    const menu = readSource('layouts/shared/MenuGrid.tsx')
    assert.equal(menu.includes("t('cart.in_cart'"), true)
    assert.equal(menu.includes('item.item_name'), true)

    const app = readSource('App.tsx')
    assert.equal(app.includes("t('status.starting')"), true)
    assert.equal(app.includes("t('errors.device_start')"), true)

    const session = readSource('hooks/useOrderingSession.ts')
    for (const key of ['missing_code', 'load_menu', 'place_order', 'request_bill', 'pay_online']) {
      assert.equal(session.includes(`t('errors.${key}')`), true, key)
    }

    assert.equal(readSource('hooks/useDeviceBootstrap.ts').includes("t('errors.device_bootstrap')"), true)

    const main = readSource('main.tsx')
    assert.equal(main.includes('initI18n'), true)
    assert.equal(main.includes('applyDocumentLocale'), true)
    lessThanRender(main)

    const switcher = readSource('components/LanguageSwitcher.tsx')
    assert.equal(switcher.includes('selectGuestLanguage'), true)
    assert.equal(switcher.includes('window.location.reload'), true)
    assert.equal(switcher.includes('role="group"'), true)
    assert.equal(switcher.includes('GUEST_LANGUAGES'), true)

    const loader = readSource('i18n/loader.ts')
    assert.equal(loader.includes('locales/${lang}.json'), true)
    assert.equal(loader.includes("type: 'json'"), true)

    const staff = readSource('layouts/PortableTabletAssignment.tsx')
    assert.equal(staff.includes('Enter staff PIN'), true)
  })
})

function lessThanRender(source) {
  const initAt = source.indexOf('initI18n')
  const renderAt = source.indexOf('createRoot')
  assert.equal(initAt >= 0 && renderAt > initAt, true)
  return renderAt
}
