import type { CartEntry } from '../../hooks/useOrderingSession'
import type { MenuItem } from '../../lib/api'
import { t } from '../../i18n'
import { useGuestLanguage } from '../../components/LanguageSwitcher'

interface CartPageProps {
  cartItems: CartEntry[]
  cartCount: number
  cartTotal: number
  itemNotesEnabled?: boolean
  onIncrement: (item: MenuItem) => void
  onDecrement: (itemCode: string) => void
  onBack: () => void
  onCheckout: () => void
  className?: string
}

/**
 * Full-screen cart view — the Phase-2 alternative to CartPanel's always-on
 * side panel / MobileQRLayout's bottom sheet. Controlled component: all
 * cart state and navigation lives in useOrderingSession, this component
 * only renders it and forwards user actions via callbacks, mirroring
 * CartPanel's prop shape (`cartItems`/`cartCount`/`cartTotal`/
 * `onIncrement`/`onDecrement`) so layouts can swap between panel and
 * full-page presentation with minimal glue code.
 */
function CartPage({
  cartItems,
  cartCount,
  cartTotal,
  itemNotesEnabled = false,
  onIncrement,
  onDecrement,
  onBack,
  onCheckout,
  className,
}: CartPageProps) {
  useGuestLanguage()
  return (
    <div className={className ?? 'flex h-full flex-col'}>
      <header className="flex items-center gap-2 border-b p-4">
        <button
          onClick={onBack}
          className="rounded-md border px-3 py-2 text-sm font-medium"
          aria-label={t('nav.back_to_menu')}
        >
          ← {t('nav.back_to_menu')}
        </button>
        <h1 className="flex-1 text-center text-lg font-semibold">{t('cart.your_cart')}</h1>
        <span className="w-[92px]" aria-hidden="true" />
      </header>

      <div className="flex-1 overflow-y-auto p-4">
        {cartItems.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <p className="text-muted-foreground">{t('cart.page_empty')}</p>
            <button onClick={onBack} className="rounded-md border px-4 py-2 text-sm font-medium">
              {t('nav.browse_menu')}
            </button>
          </div>
        ) : (
          <ul className="space-y-3 text-sm">
            {cartItems.map((entry) => (
              <li key={entry.item.item} className="rounded-lg border p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="flex-1 font-medium">{entry.item.item_name}</span>
                  <span className="flex items-center gap-2">
                    <button
                      onClick={() => onDecrement(entry.item.item)}
                      className="h-7 w-7 rounded-full border text-sm leading-none"
                      aria-label={t('cart.remove_one', { name: entry.item.item_name })}
                    >
                      −
                    </button>
                    <span className="w-4 text-center">{entry.qty}</span>
                    <button
                      onClick={() => onIncrement(entry.item)}
                      className="h-7 w-7 rounded-full border text-sm leading-none"
                      aria-label={t('cart.add_one', { name: entry.item.item_name })}
                    >
                      +
                    </button>
                  </span>
                </div>
                {itemNotesEnabled && entry.comment && (
                  <p className="mt-1 text-xs text-muted-foreground">{t('cart.note', { note: entry.comment })}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="border-t p-4">
        <div className="mb-3 flex items-center justify-between text-sm font-semibold">
          <span>{t(cartCount === 1 ? 'cart.count_one' : 'cart.count_many', { count: cartCount })}</span>
          <span>{cartTotal}</span>
        </div>
        <button
          onClick={onCheckout}
          disabled={cartCount === 0}
          className="w-full rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
        >
          {t('cart.proceed')}
        </button>
      </div>
    </div>
  )
}

export default CartPage
