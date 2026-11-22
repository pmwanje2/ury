import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import MenuCard from './MenuCard';
import type { ItemAvailability } from '../lib/availability-api';

vi.mock('@ury/core', () => ({ formatCurrency: (amount: number) => `Rs. ${amount}` }));

const baseProps = {
  id: '1', name: 'Chicken Biryani', price: 250, item_image: null, course: 'Main Course', item: 'ITEM-BIRYANI',
};
const entry = (fields: Partial<ItemAvailability>): ItemAvailability => ({
  item_code: baseProps.item, sellable: true, available_qty: null, reason_code: 'AVAILABLE',
  stock_policy_mode: 'NATIVE', source: 'CATALOG', production_policy: null, company: 'C1', branch: 'B1',
  department: null, production_unit: null, warehouse: null, plan_qty: null, plan_remaining: null,
  fg_available: null, max_producible: null, blocking_component: null, as_of: '', ...fields,
});

describe('MenuCard availability gating', () => {
  it('lets a cashier mark a dish sold out without adding it to an order', async () => {
    const update = vi.fn().mockResolvedValue(undefined);
    const onClick = vi.fn();
    render(<MenuCard {...baseProps} onClick={onClick} availability={entry({})}
      availabilityActions={{ canRestore: false, update }} />);
    await userEvent.click(screen.getByRole('button', { name: 'Manage availability' }));
    expect(screen.queryByRole('button', { name: 'Set count' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Mark sold out' }));
    expect(update).toHaveBeenCalledWith({ status: 'Sold Out' });
    expect(onClick).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toMatch(/\b(?:URY|Frappe|ERPNext|Kamra)\b/i);
  });

  it('lets a manager restore and set a count on a blocked card', async () => {
    const update = vi.fn().mockResolvedValue(undefined);
    render(<MenuCard {...baseProps} availability={entry({ sellable: false, reason_code: 'SOLD_OUT_MANUAL' })}
      availabilityActions={{ canRestore: true, update }} />);
    await userEvent.click(screen.getByRole('button', { name: 'Manage availability' }));
    await userEvent.click(screen.getByRole('button', { name: 'Available again' }));
    expect(update).toHaveBeenCalledWith({ status: 'Available' });
    await userEvent.type(screen.getByLabelText('Daily count'), '7');
    await userEvent.click(screen.getByRole('button', { name: 'Set count' }));
    expect(update).toHaveBeenCalledWith({ count: 7, reset_policy: 'Start of business day' });
  });
  it('keeps an unconstrained native item clickable with no chip', async () => {
    const onClick = vi.fn();
    render(<MenuCard {...baseProps} onClick={onClick} availability={entry({})} />);
    expect(screen.queryByText(/available|sold out/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByText('Chicken Biryani'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('keeps the positive chip for unconstrained Tier 2 items', () => {
    render(<MenuCard {...baseProps} availability={entry({ stock_policy_mode: 'RESERVATION_CONTROLLED', source: 'PRODUCTION' })} />);
    expect(screen.getByText('Available')).toBeInTheDocument();
  });

  it('disables the card and shows the reason message for a PLAN_EXHAUSTED item', async () => {
    const onClick = vi.fn();
    render(<MenuCard {...baseProps} onClick={onClick} availability={entry({
      sellable: false, available_qty: 0, reason_code: 'PLAN_EXHAUSTED',
    })} />);
    expect(screen.getByText('Sold out')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Chicken Biryani'));
    expect(onClick).not.toHaveBeenCalled();
  });
});
