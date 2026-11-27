import { resolveBrand } from '@ury/core';
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useIdleReset } from '../hooks/useIdleReset'
import { useOrderingSession } from '../hooks/useOrderingSession'
import type { OrderingContext } from '../lib/api'
import { menuCategories } from '../lib/menu-categories'
import { t } from '../i18n'
import { locationLabel } from '../i18n/location-label'
import LanguageSwitcher, { useGuestLanguage } from '../components/LanguageSwitcher'
import CategoryTabs from './shared/CategoryTabs'
import SearchBar from './shared/SearchBar'
import MenuGrid from './shared/MenuGrid'
import ProductDetail from './shared/ProductDetail'
import CartPage from './shared/CartPage'
import CheckoutScreen from './shared/CheckoutScreen'
import OrderStatusScreen from './shared/OrderStatusScreen'

const OTHER = '__other__'

interface LayoutProps {
  initialContext?: OrderingContext
}

function MobileQRLayout({ initialContext }: LayoutProps) {
  useGuestLanguage()
  const {
    context,
    menu,
    availability,
    order,
    orderStatus,
    cart,
    loading,
    submitting,
    error,
    billRequested,
    requestingAssistance,
    assistanceCooldown,
    assistanceMessage,
    handleRequestAssistance,
    paymentRequest,
    payingOnline,
    screen,
    detailItemCode,
    addToCart,
    decrementCart,
    submitCart,
    handleRequestBill,
    payOnline,
    resetSession,
    cartItems,
    cartCount,
    cartTotal,
    goToMenu,
    goToDetail,
    goToCart,
    goToCheckout,
    goToStatus,
  } = useOrderingSession(initialContext)

  // Local state for category and search filters
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')

  // Categories in the menu's configured order (#98); "Other" is translated per render.
  const groups = useMemo(() => menuCategories(menu), [menu])
  const categories = groups.map((group) => ({ course: group.course ?? OTHER, course_label: group.label ?? t('menu.other') }))

  // Filter menu by category and search
  const filteredMenu = useMemo(() => {
    let filtered = groups.flatMap((group) => group.items)
    if (selectedCategory !== null) {
      filtered = groups.find((group) => (group.course ?? OTHER) === selectedCategory)?.items ?? []
    }
    if (searchQuery) {
      const lowerQuery = searchQuery.toLowerCase()
      filtered = filtered.filter((item) => item.item_name.toLowerCase().includes(lowerQuery))
    }
    return filtered
  }, [groups, selectedCategory, searchQuery])

  // The category chips stick below this sticky header, whose height
  // changes with the language switcher and the location label.
  const headerRef = useRef<HTMLElement>(null)
  const [headerHeight, setHeaderHeight] = useState(0)
  useLayoutEffect(() => {
    const measure = () => setHeaderHeight(headerRef.current?.offsetHeight ?? 0)
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  })

  function handleStartOver() {
    if (window.confirm(t('confirm.start_over'))) {
      resetSession()
    }
  }

  // Idle-reset: for a QR/link session (no device credential) resetSession
  // clears storage and falls back to the "missing ordering code" error,
  // requiring a fresh scan — resetSession already encodes that distinction,
  // so no separate device/QR branch is needed here.
  useIdleReset(resetSession, (context?.session_idle_timeout_minutes ?? 30) * 60000)

  // Handle product card click — check if detail is enabled
  function handleProductClick(itemCode: string) {
    if (context?.capabilities.product_detail_enabled) {
      goToDetail(itemCode)
    } else {
      // Add to cart directly if detail is disabled
      const item = menu.find((m) => m.item === itemCode)
      if (item) addToCart(item)
    }
  }

  if (loading) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 text-muted-foreground">
        <LanguageSwitcher />
        {t('status.loading_menu')}
      </div>
    )
  }

  if (error && !context) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
        <LanguageSwitcher />
        <p className="text-destructive">{error}</p>
      </div>
    )
  }

  // `source` (not the absence of a table) is the authoritative signal for
  // pickup mode — set server-side by _verify_qr_token/_resolve_device, never
  // guessed from context.table being falsy.
  const brand = resolveBrand({ name: '', logo: '' });
  const isPickup = context?.source === 'QR Pickup'

  // Menu screen: header + category tabs + search bar + product grid
  if (screen === 'menu') {
    return (
      <div className="min-h-screen pb-28">
        <header ref={headerRef} className="sticky top-0 z-20 border-b bg-background/95 px-4 py-3 backdrop-blur">
          {(brand.name || brand.logo) && <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
            {brand.logo && <img src={brand.logo} alt="" className="h-8 w-auto max-w-32 object-contain" />}
            {brand.name && <span className="break-words">{brand.name}</span>}
          </div>}
          <div className="flex items-center justify-between gap-2">
            <h1 className="text-lg font-semibold">
              {isPickup ? t('header.pickup') : context?.table ? locationLabel(context.table, context.area) : t('header.order')}
            </h1>
            <button
              onClick={handleStartOver}
              className="rounded-md border px-2 py-1 text-xs font-medium text-muted-foreground"
            >
              {t('actions.start_over')}
            </button>
          </div>
          <div className="mt-2">
            <LanguageSwitcher />
          </div>
        </header>

        {error && (
          <div className="mx-4 mt-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{error}</div>
        )}

        {order && order.items.length > 0 && (
          <section className="mx-4 mt-4 rounded-lg border p-3">
            <h2 className="mb-2 text-sm font-medium text-muted-foreground">{t('order.so_far')}</h2>
            {isPickup && order.pickup_code && (
              <p className="mb-2 rounded-md bg-muted p-2 text-center text-sm font-semibold">
                {t('order.pickup_code', { code: order.pickup_code })}
              </p>
            )}
            <ul className="space-y-1 text-sm">
              {order.items.map((row, idx) => (
                <li key={`${row.item_code}-${idx}`} className="flex justify-between">
                  <span>{row.item_name} × {row.qty}</span>
                  <span>{row.amount}</span>
                </li>
              ))}
            </ul>
            <div className="mt-2 flex justify-between border-t pt-2 text-sm font-semibold">
              <span>{t('order.total')}</span>
              <span>{order.grand_total}</span>
            </div>
            {!isPickup && context?.capabilities.request_bill_enabled && !order.billed && (
              <>
                <button
                  className="mt-3 min-h-11 w-full rounded-md border py-2 text-sm font-medium disabled:opacity-50"
                  disabled={requestingAssistance || assistanceCooldown}
                  onClick={handleRequestAssistance}
                >
                  {requestingAssistance ? t('assistance.calling') : t('assistance.call')}
                </button>
                {assistanceMessage && <p role="status" className="mt-2 text-sm">{assistanceMessage}</p>}
              </>
            )}
            {context?.capabilities.feedback_enabled && order.billed && (
              <a
                href="/order/feedback"
                className="mt-3 block w-full rounded-md border py-2 text-center text-sm font-medium"
              >
                {t('feedback.rate_visit')}
              </a>
            )}
          </section>
        )}

        <CategoryTabs
          categories={categories}
          activeCourse={selectedCategory}
          onSelect={setSelectedCategory}
          allLabel={t('menu.all_items')}
          stickyTop={headerHeight}
        />

        <div className="mx-4 mt-4">
          <SearchBar value={searchQuery} onChange={setSearchQuery} placeholder={t('search.placeholder')} />
        </div>

        {/* MenuGrid layers V3-44 availability gating (sold-out badge/disabled
            state) on top of the shared ProductCard — same pattern as
            TabletLayout/LandscapeKioskLayout. */}
        <MenuGrid
          menu={filteredMenu}
          cart={cart}
          showImage={context?.capabilities.show_item_images ?? false}
          onItemClick={(item) => handleProductClick(item.item)}
          gridClassName="mx-4 mt-4 grid grid-cols-2 gap-3"
          cardClassName="flex flex-col cursor-pointer overflow-hidden rounded-lg border transition active:scale-[0.98]"
          imageClassName="h-24 w-full object-cover"
          availability={availability}
        />

        {cartCount > 0 && (
          <div className="fixed inset-x-0 bottom-0 border-t bg-background p-4">
            <button
              onClick={goToCart}
              className="w-full rounded-md bg-primary py-3 font-medium text-primary-foreground"
            >
              {t('cart.view', { items: t(cartCount === 1 ? 'cart.count_one' : 'cart.count_many', { count: cartCount }), total: cartTotal })}
            </button>
          </div>
        )}
      </div>
    )
  }

  // Detail screen
  if (screen === 'detail' && detailItemCode) {
    const menuItem = menu.find((m) => m.item === detailItemCode)
    if (!menuItem) {
      return (
        <div className="flex h-screen items-center justify-center text-destructive">
          {t('item.not_found')}
        </div>
      )
    }
    return (
      <ProductDetail
        session={context!.session}
        itemCode={detailItemCode}
        menuItem={menuItem}
        onAddToCart={addToCart}
        onBack={goToMenu}
        menu={menu}
        itemNotesEnabled={context?.capabilities.item_notes_enabled ?? false}
      />
    )
  }

  // Cart screen
  if (screen === 'cart') {
    return (
      <CartPage
        cartItems={cartItems}
        cartCount={cartCount}
        cartTotal={cartTotal}
        itemNotesEnabled={context?.capabilities.item_notes_enabled ?? false}
        onIncrement={addToCart}
        onDecrement={decrementCart}
        onBack={goToMenu}
        onCheckout={goToCheckout}
        className="flex h-screen flex-col"
      />
    )
  }

  // Checkout screen
  if (screen === 'checkout') {
    return (
      <div className="flex h-screen flex-col overflow-y-auto">
        <header className="flex items-center gap-2 border-b p-4">
          <button
            onClick={goToCart}
            className="rounded-md border px-3 py-2 text-sm font-medium"
            aria-label={t('nav.back_to_cart')}
          >
            ← {t('nav.back_to_cart')}
          </button>
          <h1 className="flex-1 text-center text-lg font-semibold">{t('checkout.title')}</h1>
          <span className="w-[92px]" aria-hidden="true" />
        </header>
        <div className="mx-4 mt-2">
          <LanguageSwitcher />
        </div>
        {error && (
          <div className="mx-4 mt-3 whitespace-pre-line rounded-md bg-destructive/10 p-3 text-sm text-destructive">{error}</div>
        )}
        <div className="flex-1 overflow-y-auto pb-4">
          <CheckoutScreen
            capabilities={{
              tip_prompt: context?.capabilities.tip_prompt,
              customer_payment_enabled: context?.capabilities.customer_payment_enabled ?? false,
              pay_at_counter_enabled: context?.capabilities.pay_at_counter_enabled ?? false,
              request_bill_enabled: context?.capabilities.request_bill_enabled ?? false,
            }}
            cartItems={cartItems}
            cartCount={cartCount}
            cartTotal={cartTotal}
            submitting={submitting}
            payingOnline={payingOnline}
            billRequested={billRequested}
            paymentRequest={paymentRequest}
            onSubmitCart={(tip) => {
              submitCart(tip).then((success) => {
                if (success) goToStatus()
              })
            }}
            onPayOnline={async (tip) => {
              if (cartItems.length > 0 && !(await submitCart(tip))) return
              payOnline()
            }}
            onRequestBill={handleRequestBill}
          />
        </div>
      </div>
    )
  }

  // Status screen
  if (screen === 'status') {
    const canAddMore = context?.capabilities.add_to_running_table_enabled ?? false
    return (
      <OrderStatusScreen
        status={orderStatus}
        isPickup={isPickup}
        pickupCode={isPickup ? order?.pickup_code : undefined}
        canAddMore={canAddMore && !isPickup}
        onAddMore={() => {
          goToMenu()
          setSelectedCategory(null)
          setSearchQuery('')
        }}
        onDone={resetSession}
      />
    )
  }

  return null
}

export default MobileQRLayout
