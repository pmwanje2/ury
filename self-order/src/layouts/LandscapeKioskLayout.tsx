import { useMemo, useRef, useState } from 'react'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@ury/ui'
import { useIdleReset } from '../hooks/useIdleReset'
import { useOrderingSession } from '../hooks/useOrderingSession'
import type { OrderingContext } from '../lib/api'
import { menuCategories } from '../lib/menu-categories'
import { t } from '../i18n'
import { locationLabel } from '../i18n/location-label'
import LanguageSwitcher, { useGuestLanguage } from '../components/LanguageSwitcher'
import CartPanel from './shared/CartPanel'
import CartPage from './shared/CartPage'
import CategoryTabs from './shared/CategoryTabs'
import CheckoutScreen from './shared/CheckoutScreen'
import MenuGrid from './shared/MenuGrid'
import OrderStatusScreen from './shared/OrderStatusScreen'
import ProductDetail from './shared/ProductDetail'
import SearchBar from './shared/SearchBar'

const IDLE_RESET_GRACE_MS = 15000
const OTHER = '__other__'

interface LayoutProps {
  initialContext?: OrderingContext
}

/**
 * Large horizontally-oriented self-service kiosk screen (entrance /
 * counter / food-court) — used standing, at arm's length, by an unassisted
 * customer. Wider menu grid than TabletLayout (more columns, bigger touch
 * targets, more spacing) with a persistent cart panel always visible on the
 * right so the order is a one-glance affair.
 *
 * Layout: horizontal category tabs + searchbar above a wide product grid,
 * with a persistent right-side cart panel that shows order summary + payment
 * options. Clicking a product either adds it to cart directly (if
 * product_detail_enabled is false) or opens a detail modal (if enabled).
 * Checkout and status screens navigate via the screen state machine. Menu
 * items go through MenuGrid, which layers V3-44 stock-availability gating
 * (dims/disables out-of-stock items) on top of the shared ProductCard.
 */
function LandscapeKioskLayout({ initialContext }: LayoutProps) {
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
    payingOnline,
    paymentRequest,
    screen,
    detailItemCode,
    addToCart,
    decrementCart,
    submitCart,
    handleRequestBill,
    payOnline,
    resetSession,
    goToMenu,
    goToDetail,
    goToCart,
    goToCheckout,
    goToStatus,
    cartItems,
    cartCount,
    cartTotal,
  } = useOrderingSession(initialContext)

  const [showIdleWarning, setShowIdleWarning] = useState(false)
  const idleResetTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const idleWarnMs = (context?.session_idle_timeout_minutes ?? 30) * 60000
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')

  function handleReset() {
    if (window.confirm(t('confirm.new_order'))) {
      resetSession()
    }
  }

  // Unattended-kiosk protection: warn after idleWarnMs of no interaction,
  // then reset after an additional IDLE_RESET_GRACE_MS if the guest doesn't
  // respond. Never fires mid-checkout — submitting/payingOnline gate both
  // the warning and the reset itself.
  useIdleReset(() => {
    if (submitting || payingOnline) return
    setShowIdleWarning(true)
    idleResetTimerRef.current = setTimeout(() => {
      setShowIdleWarning(false)
      if (!submitting && !payingOnline) {
        resetSession()
      }
    }, IDLE_RESET_GRACE_MS)
  }, idleWarnMs)

  function handleStillHere() {
    clearTimeout(idleResetTimerRef.current)
    setShowIdleWarning(false)
  }

  // Categories in the menu's configured order (#98); "Other" is translated per render.
  const groups = useMemo(() => menuCategories(menu), [menu])
  const categories = groups.map((group) => ({ course: group.course ?? OTHER, course_label: group.label ?? t('menu.other') }))

  // Filter menu by category and search
  const visibleMenu = useMemo(() => {
    let filtered = groups.flatMap((group) => group.items)

    // Filter by category
    if (selectedCategory !== null) {
      filtered = groups.find((group) => (group.course ?? OTHER) === selectedCategory)?.items ?? []
    }

    // Filter by search
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase()
      filtered = filtered.filter(
        (item) =>
          item.item_name.toLowerCase().includes(query) ||
          (item.course_label && item.course_label.toLowerCase().includes(query)),
      )
    }

    return filtered
  }, [groups, selectedCategory, searchQuery])

  // Find the detail product
  const detailProduct = useMemo(
    () => menu.find((item) => item.item === detailItemCode),
    [menu, detailItemCode],
  )

  function handleProductClick(itemCode: string) {
    const item = menu.find((i) => i.item === itemCode)
    if (!item) return

    // If product detail is not enabled, add to cart directly
    if (!context?.capabilities.product_detail_enabled) {
      addToCart(item)
      return
    }

    // Otherwise, open the detail screen
    goToDetail(itemCode)
  }

  if (loading) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 text-xl text-muted-foreground">
        <LanguageSwitcher />
        {t('status.loading_menu')}
      </div>
    )
  }

  if (error && !context) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-10 text-center text-xl">
        <LanguageSwitcher />
        <p className="text-destructive">{error}</p>
      </div>
    )
  }

  // Cart page
  if (screen === 'cart') {
    return (
      <div className="flex h-screen flex-col">
        <header className="flex items-center justify-between border-b bg-background/95 px-10 py-6">
          <h1 className="text-3xl font-semibold">
            {context?.table ? locationLabel(context.table, context.area) : t('header.pickup')}
          </h1>
          <button
            onClick={handleReset}
            className="rounded-md border px-4 py-2 text-base font-medium text-muted-foreground"
          >
            {t('actions.new_order')}
          </button>
        </header>
        <div className="flex-1">
          <CartPage
            cartItems={cartItems}
            cartCount={cartCount}
            cartTotal={cartTotal}
            onIncrement={addToCart}
            onDecrement={decrementCart}
            onBack={goToMenu}
            onCheckout={goToCheckout}
            className="h-full"
          />
        </div>
      </div>
    )
  }

  // Checkout screen
  if (screen === 'checkout') {
    return (
      <div className="flex h-screen flex-col">
        <header className="flex items-center justify-between border-b bg-background/95 px-10 py-6">
          <h1 className="text-3xl font-semibold">
            {context?.table ? locationLabel(context.table, context.area) : t('header.pickup')}
          </h1>
          <button
            onClick={handleReset}
            className="rounded-md border px-4 py-2 text-base font-medium text-muted-foreground"
          >
            {t('actions.new_order')}
          </button>
        </header>
        <div className="flex-1 overflow-y-auto">
          <CheckoutScreen
            capabilities={
              context?.capabilities || {
                customer_payment_enabled: false,
                pay_at_counter_enabled: false,
                request_bill_enabled: false,
              }
            }
            cartItems={cartItems}
            cartCount={cartCount}
            cartTotal={cartTotal}
            submitting={submitting}
            payingOnline={payingOnline}
            billRequested={billRequested}
            paymentRequest={paymentRequest}
            onSubmitCart={() => {
              submitCart().then((success) => {
                if (success) goToStatus()
              })
            }}
            onPayOnline={async () => {
              if (cartItems.length > 0 && !(await submitCart())) return
              payOnline()
            }}
            onRequestBill={handleRequestBill}
          />
        </div>
      </div>
    )
  }

  // Order status screen
  if (screen === 'status') {
    const isPickup = !context?.table
    return (
      <div className="flex h-screen flex-col">
        <header className="flex items-center justify-between border-b bg-background/95 px-10 py-6">
          <h1 className="text-3xl font-semibold">
            {context?.table ? locationLabel(context.table, context.area) : t('header.pickup')}
          </h1>
          <button
            onClick={handleReset}
            className="rounded-md border px-4 py-2 text-base font-medium text-muted-foreground"
          >
            {t('actions.new_order')}
          </button>
        </header>
        <div className="flex-1">
          <OrderStatusScreen
            status={orderStatus}
            isPickup={isPickup}
            pickupCode={order?.pickup_code ?? undefined}
            canAddMore={context?.capabilities.add_to_running_table_enabled ?? false}
            onAddMore={goToMenu}
            onDone={handleReset}
          />
        </div>
      </div>
    )
  }

  // Menu screen (default)
  return (
    <div className="flex h-screen flex-col overflow-hidden text-lg">
      <header className="flex items-center justify-between border-b bg-background/95 px-10 py-6">
        <h1 className="text-3xl font-semibold">
          {context?.table ? locationLabel(context.table, context.area) : t('header.pickup')}
        </h1>
        <LanguageSwitcher />
        <button
          onClick={handleReset}
          className="rounded-md border px-4 py-2 text-base font-medium text-muted-foreground"
        >
          {t('actions.new_order')}
        </button>
      </header>

      {error && (
        <div className="mx-10 mt-4 rounded-md bg-destructive/10 p-4 text-base text-destructive">{error}</div>
      )}

      {/* Category tabs + search bar */}
      <div className="flex-shrink-0 border-b bg-background/95 px-10 py-4">
        <div className="flex items-center gap-4">
          <CategoryTabs
            categories={categories}
            activeCourse={selectedCategory}
            onSelect={setSelectedCategory}
            allLabel={t('menu.all_items')}
          />
        </div>
        <div className="mt-3 w-80">
          <SearchBar value={searchQuery} onChange={setSearchQuery} placeholder={t('search.placeholder')} />
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        <main className="flex-1 overflow-y-auto p-10">
          {/* Product detail modal overlay */}
          {screen === 'detail' && detailProduct && context ? (
            <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/50">
              <div className="h-5/6 w-2/3 overflow-hidden rounded-lg bg-background shadow-lg">
                <ProductDetail
                  session={context.session}
                  itemCode={detailItemCode || ''}
                  menuItem={detailProduct}
                  onAddToCart={addToCart}
                  onBack={goToMenu}
                  menu={menu}
                  itemNotesEnabled={context?.capabilities.item_notes_enabled ?? false}
                />
              </div>
            </div>
          ) : null}

          {/* Product grid — MenuGrid layers V3-44 availability gating on
              top of the shared ProductCard. */}
          <MenuGrid
            menu={visibleMenu}
            cart={cart}
            showImage={context?.capabilities.show_item_images ?? false}
            onItemClick={(item) => handleProductClick(item.item)}
            gridClassName="grid grid-cols-4 gap-6 xl:grid-cols-5"
            cardClassName="flex min-w-[180px] flex-col overflow-hidden rounded-2xl border text-left text-lg transition active:scale-[0.97]"
            imageClassName="h-40 w-full object-cover"
            availability={availability}
          />

          {visibleMenu.length === 0 && (
            <div className="flex items-center justify-center py-20 text-muted-foreground">
              {searchQuery ? t('menu.no_match') : t('menu.empty')}
            </div>
          )}
        </main>

        <CartPanel
          context={context}
          order={order}
          cartItems={cartItems}
          cartCount={cartCount}
          cartTotal={cartTotal}
          submitting={submitting}
          billRequested={billRequested}
          payingOnline={payingOnline}
          paymentRequest={paymentRequest}
          onIncrement={addToCart}
          onDecrement={decrementCart}
          onSubmit={goToCart}
          onRequestBill={handleRequestBill}
          onPayOnline={async () => {
            if (cartItems.length > 0 && !(await submitCart())) return
            payOnline()
          }}
          className="flex w-[420px] shrink-0 flex-col overflow-hidden border-l bg-background p-6 text-base"
        />
      </div>

      <Dialog open={showIdleWarning} onOpenChange={(open) => !open && handleStillHere()}>
        <DialogContent onClose={handleStillHere} closeLabel={t('dialog.close')}>
          <DialogHeader>
            <DialogTitle>{t('cart.still_there')}</DialogTitle>
          </DialogHeader>
          <DialogFooter>
            <button
              onClick={handleStillHere}
              className="w-full rounded-md bg-primary py-3 text-base font-medium text-primary-foreground"
            >
              {t('cart.still_here')}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default LandscapeKioskLayout
