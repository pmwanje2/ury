import { AvailabilityActionMenu, MenuItemCard, type AvailabilityActions } from '@ury/ui'
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
  availabilityActions?: AvailabilityActions
  onClick?: () => void
  onConfigure?: () => void
  quantity?: number
  onIncrement?: () => void
  onDecrement?: () => void
  incrementDisabled?: boolean
  decrementDisabled?: boolean
}

const availabilityLabels = {
  manage: 'Manage availability',
  soldOut: 'Mark sold out',
  restore: 'Available again',
  dailyCount: 'Daily count',
  setCount: 'Set count',
  reset: 'Reset at first opening',
  error: 'Could not update availability. Try again.',
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
  availabilityActions,
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

  const card = (
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
  if (!availabilityActions) return card
  return <div className="relative">
    {card}
    <div className="absolute right-1 top-1 z-10">
      <AvailabilityActionMenu actions={availabilityActions} labels={availabilityLabels}
        manualSoldOut={availability?.manual_status === 'Sold Out' || availability?.reason_code === 'SOLD_OUT_MANUAL'}
        dailyLimit={availability?.daily_limit} autoReset={availability?.auto_reset_on_open} />
    </div>
  </div>
}
