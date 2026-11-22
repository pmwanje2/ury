import { MenuItemCard } from '@ury/ui'
import { formatCurrency } from '@ury/core'
import {
  getAvailabilityMessage,
  isUnavailable,
  type ItemAvailability,
} from '../lib/availability-api'

interface Props {
  name: string
  price: number
  imageUrl: string | null
  course?: string
  disabled?: boolean
  availability?: ItemAvailability
  onClick?: () => void
  onConfigure?: () => void
  quantity?: number
  onIncrement?: () => void
  onDecrement?: () => void
  incrementDisabled?: boolean
  decrementDisabled?: boolean
}

function getAvailabilityTag(
  availability: ItemAvailability | undefined,
  unavailableMessage: string | null
): {
  variant: 'tagDestructive' | 'tagWarning' | 'tagSuccess'
  text: string
  showDot: boolean
} | null {
  if (!availability) return null

  if (unavailableMessage) {
    return {
      variant: 'tagDestructive',
      text: unavailableMessage,
      showDot: false,
    }
  }

  if (availability.available_qty != null && availability.available_qty < 5) {
    return {
      variant: 'tagWarning',
      text: `${availability.available_qty} left`,
      showDot: false,
    }
  }

  if (availability.available_qty == null) {
    if (availability.stock_policy_mode === 'NATIVE' && availability.source === 'CATALOG') return null
    return { variant: 'tagSuccess', text: 'Available', showDot: true }
  }

  return {
    variant: 'tagSuccess',
    text: `${availability.available_qty} left`,
    showDot: true,
  }
}

/** Menu card with optional availability badge (display-only; sync_order is authority). */
export default function ServeMenuCard({
  name,
  price,
  imageUrl,
  course,
  disabled,
  availability,
  onClick,
  onConfigure,
  quantity,
  onIncrement,
  onDecrement,
  incrementDisabled,
  decrementDisabled,
}: Props) {
  const unavailableMessage = isUnavailable(availability)
    ? getAvailabilityMessage(availability?.reason_code)
    : null
  const availabilityTag = getAvailabilityTag(availability, unavailableMessage)

  return (
    <MenuItemCard
      name={name}
      priceLabel={formatCurrency(price)}
      imageUrl={imageUrl}
      course={course}
      onClick={onClick}
      onConfigure={onConfigure}
      disabled={disabled}
      unavailableMessage={unavailableMessage}
      availabilityTag={availabilityTag}
      quantity={quantity}
      onIncrement={onIncrement}
      onDecrement={onDecrement}
      incrementDisabled={incrementDisabled}
      decrementDisabled={decrementDisabled}
    />
  )
}
