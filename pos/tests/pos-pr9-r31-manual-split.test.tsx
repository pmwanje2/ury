import { fireEvent, render, screen } from '@testing-library/react';
import { formatCurrency } from '@ury/core';
import { expect, it, vi } from 'vitest';
import BillSplitDialog, { type BillSplitItem } from '../src/components/BillSplitDialog';

vi.mock('../src/i18n', () => ({ t: (key: string) => key }));
vi.mock('../src/components/CustomerPicker', () => ({ CustomerPicker: () => null }));

function preview(currency: string, items: BillSplitItem[], qty: number) {
  render(<BillSplitDialog open invoiceName="Check 1" currency={currency} items={items}
    onOpenChange={vi.fn()} onConfirm={vi.fn()} onConfirmMulti={vi.fn()} />);
  fireEvent.change(screen.getByRole('spinbutton', { name: /Soup/ }), { target: { value: String(qty) } });
}

const soup = { name: 'soup', item_name: 'Soup', qty: 1, rate: 8, amount: 8, reservation_line_key: 'dish' };
const tea = { name: 'tea', item_name: 'Tea', qty: 1, rate: 4, amount: 4 };
const side = { name: 'side', item_name: 'Dressing', qty: 1, rate: 2, amount: 2,
  custom_parent_line_key: 'dish', custom_is_modifier: 1 };

it.each(['UGX', 'USD', 'KWD'])('includes the linked modifier when moving the entire dish in %s', currency => {
  preview(currency, [soup, tea, side], 1);
  expect(screen.getByText(formatCurrency(10, currency))).toBeVisible();
  expect(screen.getByText('1 × Tea')).toBeVisible();
});

it.each([['UGX', 3], ['USD', 3.25], ['KWD', 3.125]] as const)(
  'prorates child and grandchild quantities on a partial %s split', (currency, toppingRate) => {
    const topping = { name: 'oil', item_name: 'Oil', qty: 3, rate: toppingRate, amount: 3 * toppingRate,
      custom_parent_line_key: 'side', custom_is_modifier: 1 };
    preview(currency, [topping, { ...soup, qty: 3, amount: 24 }, tea, { ...side, qty: 6, amount: 12 }], 1);
    expect(screen.getByText(formatCurrency(8 + 2 * 2 + toppingRate, currency))).toBeVisible();
  },
);
