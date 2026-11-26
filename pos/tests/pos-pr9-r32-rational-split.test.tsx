import { fireEvent, render, screen } from '@testing-library/react';
import { formatCurrency } from '@ury/core';
import { describe, expect, it, vi } from 'vitest';
import BillSplitDialog from '../src/components/BillSplitDialog';

vi.mock('../src/i18n', () => ({ t: (key: string) => key }));
vi.mock('../src/components/CustomerPicker', () => ({ CustomerPicker: () => null }));

describe.each([['UGX', 1], ['USD', 100], ['KWD', 1000]] as const)(
  'manual split in %s', (currency, scale) => {
    it.each([
      { qty: 14, move: 5, linkedQty: 7, minor: 21, share: 8 },
      { qty: 14, move: 5, linkedQty: 7, minor: -21, share: -8 },
      { qty: 3, move: 1, linkedQty: 3, minor: 5, share: 2 },
      { qty: 7, move: 2, linkedQty: 7, minor: 5, share: 1 },
      // The exact share is 100000000.499999995... minor units, just below half.
      { qty: 10000.0001, move: 5000, linkedQty: 10000.0001, minor: 200000003, share: 100000000 },
    ])('allocates $move of $qty with $minor signed minor units', ({ qty, move, linkedQty, minor, share }) => {
      const rate = qty < 100 ? 1 : 0;
      render(<BillSplitDialog open invoiceName="Split" currency={currency} onOpenChange={vi.fn()}
        onConfirm={vi.fn()} onConfirmMulti={vi.fn()} items={[
          { name: 'dish', item_name: 'Dish', qty, rate, amount: rate * qty },
          { name: 'side', item_name: 'Side', qty: linkedQty, rate: minor / scale / linkedQty,
            amount: minor / scale, custom_parent_line_key: 'dish', custom_is_modifier: 1 },
        ]} />);
      fireEvent.change(screen.getByRole('spinbutton', { name: /Dish/ }), { target: { value: String(move) } });
      expect(screen.getByText(formatCurrency(move * rate + share / scale, currency))).toBeVisible();
    });
  },
);
