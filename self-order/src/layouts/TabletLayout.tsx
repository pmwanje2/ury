import { useRef, useState } from 'react'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@ury/ui'
import { useIdleReset } from '../hooks/useIdleReset'
import { useOrderingSession } from '../hooks/useOrderingSession'
import type { OrderingContext } from '../lib/api'
import { t } from '../i18n'
import LanguageSwitcher, { useGuestLanguage } from '../components/LanguageSwitcher'
import CartPanel from './shared/CartPanel'
import MenuGrid from './shared/MenuGrid'

const IDLE_WARN_MS = 60000
const IDLE_RESET_GRACE_MS = 15000

interface LayoutProps {
  initialContext?: OrderingContext
}

/**
 * Two-pane layout for a tablet, portrait or landscape, handed to a customer
 * or fixed at a table. Left/main pane is a scrollable menu grid; right pane
 * is a persistent order summary + cart (not a bottom sheet like
 * MobileQRLayout — always visible, no toggle).
 */
function TabletLayout({ initialContext }: LayoutProps) {
  useGuestLanguage()
  const {
    context,
    menu,
    order,
    cart,
    loading,
    submitting,
    error,
    billRequested,
    payingOnline,
    addToCart,
    decrementCart,
    submitCart,
    handleRequestBill,
    payOnline,
    resetSession,
    cartItems,
    cartCount,
    cartTotal,
  } = useOrderingSession(initialContext)

  const [showIdleWarning, setShowIdleWarning] = useState(false)
  const idleResetTimerRef = useRef<ReturnType<typeof setTimeout>>()

  function handleReset() {
    if (window.confirm(t('confirm.new_order'))) {
      resetSession()
    }
  }

  // Unattended-kiosk protection: warn after IDLE_WARN_MS of no interaction,
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
  }, IDLE_WARN_MS)

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

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="flex items-center justify-between border-b bg-background/95 px-6 py-4">
        <h1 className="text-xl font-semibold">
          {context?.table ? t('header.table', { table: context.table }) : t('header.pickup')}
        </h1>
        <button
          onClick={handleReset}
          className="rounded-md border px-3 py-2 text-sm font-medium text-muted-foreground"
        >
          {t('actions.new_order')}
        </button>
        <LanguageSwitcher />
      </header>

      {error && (
        <div className="mx-6 mt-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{error}</div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <main className="w-[68%] overflow-y-auto p-6">
          <MenuGrid
            menu={menu}
            cart={cart}
            capabilities={context?.capabilities}
            onAdd={addToCart}
            gridClassName="grid grid-cols-3 gap-4 lg:grid-cols-4"
            cardClassName="flex min-w-[150px] flex-col overflow-hidden rounded-xl border text-left transition active:scale-[0.98]"
            imageClassName="h-32 w-full object-cover"
          />
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
          onIncrement={addToCart}
          onDecrement={decrementCart}
          onSubmit={submitCart}
          onRequestBill={handleRequestBill}
          onPayOnline={payOnline}
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
