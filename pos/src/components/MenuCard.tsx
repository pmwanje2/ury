import type { FC } from 'react';
import { MenuItemCard } from '@ury/ui';
import { formatCurrency } from '@ury/core';
import { t } from '../i18n';
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
}

const MenuCard: FC<MenuCardProps> = ({
  name,
  price,
  item_image,
  course,
  onClick,
  disabled,
  availability,
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

  return (
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
};

export default MenuCard;
