import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { ItemAvailability } from '../lib/availability-api';

vi.mock('@ury/core', () => ({
  formatCurrency: (amount: number) => `Rs. ${amount}`,
}));

import MenuCard from './MenuCard';

const baseProps = {
  id: '1',
  name: 'Chicken Biryani',
  price: 250,
  item_image: null,
  course: 'Main Course',
  item: 'ITEM-BIRYANI',
};

describe('MenuCard availability gating', () => {
  it('stays clickable and shows no badge for a sellable item', async () => {
    const availability = {
      item_code: 'ITEM-BIRYANI',
      sellable: true,
      available_qty: 5,
      reason_code: 'AVAILABLE',
    } as ItemAvailability;
    const onClick = vi.fn();

    render(<MenuCard {...baseProps} onClick={onClick} availability={availability} />);

    expect(screen.queryByText(/sold out|not available|unavailable/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByText('Chicken Biryani'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('disables the card and shows the reason message for a PLAN_EXHAUSTED item', async () => {
    const availability = {
      item_code: 'ITEM-BIRYANI',
      sellable: false,
      available_qty: 0,
      reason_code: 'PLAN_EXHAUSTED',
    } as ItemAvailability;
    const onClick = vi.fn();

    render(<MenuCard {...baseProps} onClick={onClick} availability={availability} />);

    expect(await screen.findByText('Sold out')).toBeInTheDocument();

    await userEvent.click(screen.getByText('Chicken Biryani'));
    expect(onClick).not.toHaveBeenCalled();
  });
});
