import type { MenuItem } from '../../lib/api'
import { getAvailabilityMessage, isUnavailable, type MenuAvailability } from '../../lib/availability'
import ProductCard from './ProductCard'

type Cart = Record<string, { item: MenuItem; qty: number }>

interface MenuGridProps {
  menu: MenuItem[]
  cart: Cart
  availability: MenuAvailability
  showImage: boolean
  onItemClick: (item: MenuItem) => void
  gridClassName: string
  cardClassName: string
  imageClassName?: string
}

export default function MenuGrid({
  menu, cart, availability, showImage, onItemClick, gridClassName, cardClassName, imageClassName,
}: MenuGridProps) {
  // Noted lines of the same dish are separate cart entries; badge their total.
  const qtyByItem: Record<string, number> = {}
  for (const line of Object.values(cart)) {
    qtyByItem[line.item.item] = (qtyByItem[line.item.item] ?? 0) + line.qty
  }
  return (
    <div className={gridClassName}>
      {menu.map((item) => {
        const entry = availability[item.item]
        const unavailable = isUnavailable(entry)
        return (
          <ProductCard
            key={item.item}
            item={item}
            cartQty={qtyByItem[item.item] ?? 0}
            showImage={showImage}
            onClick={() => onItemClick(item)}
            cardClassName={cardClassName}
            imageClassName={imageClassName}
            disabled={unavailable}
            unavailableMessage={unavailable ? getAvailabilityMessage(entry.reason_code) : null}
          />
        )
      })}
    </div>
  )
}
