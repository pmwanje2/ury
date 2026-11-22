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
  item_image: string | null;
  course?: string;
  item: string;
  onClick?: () => void;
  disabled?: boolean;
  availability?: ItemAvailability | null;
  availabilityActions?: AvailabilityActions;
}

const MenuCard: FC<MenuCardProps> = ({
  name,
  price,
  item_image,
  course,
  onClick,
  disabled,
  availability,
  availabilityActions,
}) => {
  const unavailable = isUnavailable(availability);
  const unavailableMessage = unavailable
    ? getAvailabilityMessage(availability?.reason_code)
    : null;

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
        text: t('menu.qty_left', { qty: availability.available_qty }),
        showDot: false,
      };
    }

    if (availability.available_qty == null) {
      if (availability.stock_policy_mode === 'NATIVE' && availability.source === 'CATALOG') return null;
      return { variant: 'tagSuccess', text: t('menu.available_chip'), showDot: true };
    }

    return {
      variant: 'tagSuccess',
      text: t('menu.qty_left', { qty: availability.available_qty }),
      showDot: true,
    };
  };

  const availabilityTag = getAvailabilityTag();

  const card = (
    <MenuItemCard
      name={name}
      priceLabel={formatCurrency(price)}
      imageUrl={item_image}
      course={course}
      onClick={onClick}
      disabled={disabled || unavailable}
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
