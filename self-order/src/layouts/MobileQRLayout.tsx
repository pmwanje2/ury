import { formatCurrency, resolveBrand } from '@ury/core'
import { hasPrice, useOrderingSession } from '../hooks/useOrderingSession'
import GuestNoteDialog from '../components/GuestNoteDialog'
import GuestAddons from '../components/GuestAddons'
import type { OrderingContext } from '../lib/api'
import { t } from '../i18n'
import LanguageSwitcher, { useGuestLanguage } from '../components/LanguageSwitcher'

interface LayoutProps {
  initialContext?: OrderingContext
}

function MobileQRLayout({ initialContext }: LayoutProps) {
  useGuestLanguage()
  const ordering = useOrderingSession(initialContext)
  const {
    context,
    menu,
    order,
    cart,
    loading,
    submitting,
    error,
    billRequested,
    requestingAssistance,
    assistanceCooldown,
    assistanceMessage,
    handleRequestAssistance,
    paymentNotice,
    payingOnline,
    addToCart,
    incrementLine,
    addonsByItem,
    retryAddons,
    decrementCart,
    submitCart,
    handleRequestBill,
    payOnline,
    resetSession,
    cartItems,
    cartCount,
    cartTotal,
  } = ordering

  function handleStartOver() {
    if (window.confirm(t('confirm.start_over'))) {
      resetSession()
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
  const isPickup = context?.source === 'QR Pickup'
  const brand = resolveBrand({ name: '', logo: '', favicon: '' })

  return (
    <div className="min-h-screen pb-28">
      <header className="sticky top-0 z-10 border-b bg-background/95 px-4 py-3 backdrop-blur">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            {(brand.name || brand.logo) && <div className="flex items-center gap-2 text-sm font-medium">
              {brand.logo && <img src={brand.logo} alt="" className="h-8 w-8 shrink-0 object-contain" />}
              <span className="break-words">{brand.name}</span>
            </div>}
            <h1 className="text-lg font-semibold">
              {isPickup ? t('header.pickup') : context?.table ? t('header.table', { table: context.table }) : t('header.order')}
            </h1>
          </div>
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
          {context?.capabilities.customer_payment_enabled && !order.billed && (
            <button
              className="mt-3 w-full rounded-md bg-primary py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
              disabled={payingOnline}
              onClick={payOnline}
            >
              {payingOnline ? t('payment.starting') : t('payment.pay_online')}
            </button>
          )}
          {paymentNotice && <p role="status" className="mt-2 text-xs text-muted-foreground">{paymentNotice}</p>}
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
            <button
              className="mt-3 w-full rounded-md border py-2 text-sm font-medium disabled:opacity-50"
              disabled={billRequested}
              onClick={handleRequestBill}
            >
              {billRequested ? t('bill.requested') : t('bill.request')}
            </button>
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

      <section className="mx-4 mt-4 grid grid-cols-2 gap-3">
        {menu.map((item) => (
          <div key={item.item} className="overflow-hidden rounded-lg border">
          <button
            disabled={!hasPrice(item)}
            onClick={() => addToCart(item)}
            className="flex w-full flex-col text-left transition active:scale-[0.98] disabled:opacity-60"
          >
            {context?.capabilities.show_item_images && item.item_image && (
              <img src={item.item_image} alt={item.item_name} className="h-24 w-full object-cover" />
            )}
            <div className="p-2">
              <div className="text-sm font-medium">{item.item_name}</div>
              <div className="text-sm text-muted-foreground tabular-nums">{hasPrice(item) ? formatCurrency(item.rate) : t('item.price_unavailable')}</div>
              {cart[item.item] && (
                <div className="mt-1 text-xs font-semibold text-primary">{t('cart.in_cart', { qty: cart[item.item].qty })}</div>
              )}
            </div>
          </button>
          <GuestAddons state={addonsByItem[item.item]} onAdd={addToCart} onRetry={() => retryAddons(item.item)} />
          </div>
        ))}
      </section>

      {cartCount > 0 && (
        <div className="fixed inset-x-0 bottom-0 border-t bg-background p-4">
          <ul className="mb-2 max-h-32 space-y-1 overflow-y-auto text-sm">
            {cartItems.map((entry) => (
              <li key={entry.key} className="flex items-center justify-between gap-2">
                <span className="min-w-0 break-words">{entry.item.item_name}{entry.note && <small className="block whitespace-pre-wrap">{entry.note}</small>}</span>
                <span className="flex items-center gap-2">
                  <button
                    onClick={() => decrementCart(entry.key)}
                    className="h-6 w-6 rounded-full border text-xs leading-none"
                    aria-label={t('cart.remove_one', { name: entry.item.item_name })}
                  >
                    −
                  </button>
                  {entry.qty}
                  <button
                    onClick={() => incrementLine(entry)}
                    className="h-6 w-6 rounded-full border text-xs leading-none"
                    aria-label={t('cart.add_one', { name: entry.item.item_name })}
                  >
                    +
                  </button>
                </span>
              </li>
            ))}
          </ul>
          <div className="mb-2 flex items-center justify-between text-sm font-semibold">
            <span>{t(cartCount === 1 ? 'cart.count_one' : 'cart.count_many', { count: cartCount })}</span>
            <span className="tabular-nums">{formatCurrency(cartTotal)}</span>
          </div>
          <button
            onClick={submitCart}
            disabled={submitting}
            className="w-full rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
          >
            {submitting ? t('order.placing') : t('order.place')}
          </button>
        </div>
      )}
      <GuestNoteDialog {...ordering} />
    </div>
  )
}

export default MobileQRLayout
