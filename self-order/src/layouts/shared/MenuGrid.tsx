import { formatCurrency } from '@ury/core'
import type { MenuItem, OrderingCapabilities } from '../../lib/api'
import { qtyForItem, type AddonChoice, type GuestCart } from '../../lib/guest-lines'
import { t } from '../../i18n'
import AddonChoices from './AddonChoices'

interface MenuGridProps {
  menu: MenuItem[]
  cart: GuestCart
  addonsByItem: Record<string, AddonChoice[]>
  capabilities: OrderingCapabilities | undefined
  onAdd: (item: MenuItem) => void
  onAddAddon: (choice: AddonChoice) => void
  /** Tailwind grid classes (columns/gap) — differs between Tablet and Kiosk. */
  gridClassName: string
  /** Tailwind classes for each card — controls touch-target size. */
  cardClassName: string
  imageClassName?: string
}

/**
 * Category-free scrollable grid of menu items. Shared visual language
 * between TabletLayout and LandscapeKioskLayout — only the sizing classes
 * differ (larger cards, more columns on the kiosk).
 */
function MenuGrid({ menu, cart, addonsByItem, capabilities, onAdd, onAddAddon, gridClassName, cardClassName, imageClassName }: MenuGridProps) {
  return (
    <div className={gridClassName}>
      {menu.map((item) => (
        <div key={item.item}>
          <button onClick={() => onAdd(item)} className={cardClassName}>
            {capabilities?.show_item_images && item.item_image && (
              <img
                src={item.item_image}
                alt={item.item_name}
                className={imageClassName ?? 'w-full object-cover'}
              />
            )}
            <div className="flex flex-1 flex-col gap-1 p-3">
              <div className="font-medium">{item.item_name}</div>
              <div className="text-muted-foreground tabular-nums">{formatCurrency(item.rate)}</div>
              {qtyForItem(cart, item.item) > 0 && (
                <div className="mt-1 text-sm font-semibold text-primary">{t('cart.in_cart', { qty: qtyForItem(cart, item.item) })}</div>
              )}
            </div>
          </button>
          <AddonChoices choices={addonsByItem[item.item] ?? []} onAdd={onAddAddon} />
        </div>
      ))}
    </div>
  )
}

export default MenuGrid
