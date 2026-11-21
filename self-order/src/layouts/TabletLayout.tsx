import { useMemo, useRef, useState } from 'react'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@ury/ui'
import { useIdleReset } from '../hooks/useIdleReset'
import { useOrderingSession } from '../hooks/useOrderingSession'
import type { MenuItem, OrderingContext } from '../lib/api'
import { menuCategories } from '../lib/menu-categories'
import { t } from '../i18n'
import { locationLabel } from '../i18n/location-label'
import LanguageSwitcher, { useGuestLanguage } from '../components/LanguageSwitcher'
import CartPanel from './shared/CartPanel'
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
 * Two-pane layout for a tablet, portrait or landscape, handed to a customer
 * or fixed at a table. Left/main pane shows category tabs + search + menu grid,
 * with product-detail support (modal/overlay or replaces grid temporarily).
 * Right pane is a persistent order summary + cart (not a bottom sheet like
 * MobileQRLayout — always visible, no toggle).
 *
 * When screen === 'checkout' or 'status', the two-pane layout gives way to
 * full-screen checkout and status screens, matching MobileQRLayout's pattern.
 */
function TabletLayout({ initialContext }: LayoutProps) {
  useGuestLanguage()
  const {
    context,
    menu,
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
    cartItems,
    cartCount,
    cartTotal,
    goToMenu,
    goToDetail,
    goToCheckout,
    goToStatus,
  } = useOrderingSession(initialContext)

  const [showIdleWarning, setShowIdleWarning] = useState(false)
  const idleResetTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const idleWarnMs = (context?.session_idle_timeout_minutes ?? 30) * 60000

  const [selectedCategory, setSelectedCategory] = useState<string | null>(null)
  const [searchTerm, setSearchTerm] = useState('')

  // Categories in the menu's configured order (#98); "Other" is translated per render.
  const groups = useMemo(() => menuCategories(menu), [menu])
  const categories = groups.map((group) => ({ course: group.course ?? OTHER, course_label: group.label ?? t('menu.other') }))

  /**
   * Filter menu by active category and search term.
   */
  const filteredMenu = useMemo(() => {
    return groups
      .filter((group) => selectedCategory === null || (group.course ?? OTHER) === selectedCategory)
      .flatMap((group) => group.items)
      .filter((item) => searchTerm === '' || item.item_name.toLowerCase().includes(searchTerm.toLowerCase()))
  }, [groups, selectedCategory, searchTerm])

  /**
   * Handle product card click: if product_detail_enabled, navigate to detail
   * screen; otherwise, add directly to cart.
   */
  const handleProductCardClick = (item: MenuItem) => {
    if (context?.capabilities.product_detail_enabled) {
      goToDetail(item.item)
    } else {
      addToCart(item)
    }
  }

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

  // Checkout screen
  if (screen === 'checkout') {
    return (
      <div className="flex h-screen flex-col overflow-hidden">
        <header className="flex items-center gap-2 border-b p-4">
          <button
            onClick={goToMenu}
            className="rounded-md border px-3 py-2 text-sm font-medium"
            aria-label={t('nav.back_to_menu')}
          >
            ← {t('nav.back_to_menu')}
          </button>
          <h1 className="flex-1 text-center text-lg font-semibold">{t('checkout.title')}</h1>
          <span className="w-[92px]" aria-hidden="true" />
        </header>
        <div className="flex-1 overflow-y-auto pb-4">
          <CheckoutScreen
            capabilities={{
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

  // Status screen
  if (screen === 'status') {
    const canAddMore = context?.capabilities.add_to_running_table_enabled ?? false
    const isPickup = context?.source === 'QR Pickup'
    return (
      <OrderStatusScreen
        status={orderStatus}
        isPickup={isPickup}
        pickupCode={isPickup ? order?.pickup_code : undefined}
        canAddMore={canAddMore && !isPickup}
        onAddMore={() => {
          goToMenu()
          setSelectedCategory(null)
          setSearchTerm('')
        }}
        onDone={resetSession}
      />
    )
  }

  // Find the menu item being displayed in detail view
  const currentMenuItem = detailItemCode
    ? menu.find((m) => m.item === detailItemCode)
    : null

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="flex items-center justify-between border-b bg-background/95 px-6 py-4">
        <h1 className="text-xl font-semibold">
          {context?.table ? locationLabel(context.table, context.area) : t('header.pickup')}
        </h1>
        <LanguageSwitcher />
        <button
          onClick={handleReset}
          className="rounded-md border px-3 py-2 text-sm font-medium text-muted-foreground"
        >
          {t('actions.new_order')}
        </button>
      </header>

      {error && (
        <div className="mx-6 mt-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <main className="w-[68%] overflow-y-auto">
          {screen === 'detail' && currentMenuItem ? (
            <ProductDetail
              session={context!.session}
              itemCode={detailItemCode!}
              menuItem={currentMenuItem}
              onAddToCart={addToCart}
              onBack={goToMenu}
              menu={menu}
              itemNotesEnabled={context?.capabilities.item_notes_enabled ?? false}
            />
          ) : (
            <div className="flex h-full flex-col p-6">
              <CategoryTabs
                categories={categories}
                activeCourse={selectedCategory}
                onSelect={setSelectedCategory}
                allLabel={t('menu.all_items')}
              />
              <div className="mb-4 mt-3">
                <SearchBar value={searchTerm} onChange={setSearchTerm} />
              </div>
              <MenuGrid
                menu={filteredMenu}
                cart={cart}
                showImage={context?.capabilities.show_item_images ?? false}
                onItemClick={handleProductCardClick}
                gridClassName="grid grid-cols-3 gap-4 lg:grid-cols-4"
                cardClassName="flex min-w-[150px] flex-col overflow-hidden rounded-xl border text-left transition active:scale-[0.98]"
                imageClassName="h-32 w-full object-cover"
                branch={context?.restaurant}
                company={context?.company}
              />
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
          onSubmit={goToCheckout}
          onRequestBill={handleRequestBill}
          onPayOnline={async () => {
            if (cartItems.length > 0 && !(await submitCart())) return
            payOnline()
          }}
          className="flex w-[32%] flex-col overflow-hidden border-l bg-background p-4"
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

export default TabletLayout
