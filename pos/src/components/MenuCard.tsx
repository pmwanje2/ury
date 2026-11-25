import type { FC } from 'react';
import { AvailabilityActionMenu, MenuItemCard, type AvailabilityActions } from '@ury/ui';
import { formatCurrency } from '@ury/core';
import { t } from '../i18n';
import { availabilityActionLabels } from '../lib/availability-labels';
import {
  getAvailabilityMessage,
  isUnavailable,
  type ItemAvailability,
} from '../lib/availability-api';

interface MenuCardProps {
  id: string;
  name: string;
  price: number;
  promoRate?: number | null;
  promoLabel?: string | null;
  unavailableDaypart?: boolean;
  daypartLabel?: string | null;
  item_image: string | null;
  course?: string;
  item: string;
  onClick?: () => void;
  disabled?: boolean;
  availability?: ItemAvailability | null;
  /** The count could not be re-read (offline or a failed refresh). */
  availabilityStale?: boolean;
  availabilityActions?: AvailabilityActions;
  currency?: string;
}

const MenuCard: FC<MenuCardProps> = ({
  name,
  price,
  promoRate,
  promoLabel,
  unavailableDaypart = false,
  daypartLabel,
  item_image,
  course,
  onClick,
  disabled,
  availability,
  availabilityStale = false,
  availabilityActions,
  currency,
}) => {
  const unavailable = isUnavailable(availability);
  const unavailableMessage = unavailable
    ? getAvailabilityMessage(availability?.reason_code)
    : null;
  // Simphony shows "[N]?" while it cannot confirm a count; a sold-out item stays blocked.
  const qtyLeft = (qty: number) => t(availabilityStale ? 'menu.qty_left_unconfirmed' : 'menu.qty_left', { qty });

  // Determine badge variant and text for availability status
  const getAvailabilityTag = (): {
    variant: 'tagDestructive' | 'tagWarning' | 'tagSuccess';
    text: string;
    showDot: boolean;
  } | null => {
    if (!availability) return null;

    if (unavailable) {
      return {
        variant: 'tagDestructive',
        text: unavailableMessage || t('menu.unavailable'),
        showDot: false,
      };
    }

    if (availability.available_qty != null && availability.available_qty < 5) {
      return {
        variant: 'tagWarning',
        text: qtyLeft(availability.available_qty),
        showDot: false,
      };
    }

    if (availability.available_qty == null) {
      if (availability.stock_policy_mode === 'NATIVE' && availability.source === 'CATALOG') return null;
      return { variant: 'tagSuccess', text: t('menu.available_chip'), showDot: true };
    }

    return {
      variant: 'tagSuccess',
      text: qtyLeft(availability.available_qty),
      showDot: true,
    };
  };

  const availabilityTag = getAvailabilityTag();
  const hasPromo = promoRate != null && Number.isFinite(promoRate) && promoRate < price;
  const effectivePriceLabel = formatCurrency(hasPromo ? promoRate! : price, currency);
  const originalPriceLabel = hasPromo ? formatCurrency(price, currency) : null;
  const effectiveDaypartLabel = unavailableDaypart
    ? (daypartLabel || t('menu.daypart_available'))
    : null;

  const card = (
    <MenuItemCard
      name={name}
      priceLabel={effectivePriceLabel}
      originalPriceLabel={originalPriceLabel}
      promoBadge={hasPromo ? (promoLabel || undefined) : undefined}
      daypartLabel={effectiveDaypartLabel}
      imageUrl={item_image}
      course={course}
      onClick={unavailableDaypart ? undefined : onClick}
      disabled={disabled || unavailable || unavailableDaypart}
      availabilityTag={availabilityTag}
    />
  );
  if (!availabilityActions) return card;
  return <div className="relative">
    {card}
    <div className="pointer-events-auto absolute right-1 top-1 z-10">
      <AvailabilityActionMenu actions={availabilityActions} labels={availabilityActionLabels()}
        manualSoldOut={availability?.manual_status === 'Sold Out' || availability?.reason_code === 'SOLD_OUT_MANUAL'}
        dailyLimit={availability?.daily_limit} autoReset={availability?.auto_reset_on_open} />
    </div>
  </div>;
};

export default MenuCard;
