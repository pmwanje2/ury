import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import BillSplitDialog from './BillSplitDialog';

vi.mock('../i18n', () => ({ t: (key: string) => key }));
vi.mock('./CustomerPicker', () => ({ CustomerPicker: () => null }));

function openSplit() {
  const onConfirmMulti = vi.fn().mockResolvedValue(undefined);
  render(<BillSplitDialog open onOpenChange={vi.fn()} invoiceName="CHECK-1"
    items={[
      { name: 'soup', item_name: 'Soup', qty: 1, rate: 8, amount: 8, custom_seat_no: 1 },
      { name: 'tea', item_name: 'Tea', qty: 1, rate: 4, amount: 4, custom_seat_no: 2 },
    ]}
    onConfirm={vi.fn()} onConfirmMulti={onConfirmMulti} />);
  return onConfirmMulti;
}

it('has a seat, even and manual split tab', () => {
  openSplit();
  for (const mode of ['by_seat', 'even', 'manual']) {
    expect(screen.getByRole('tab', { name: `floor.split_${mode}` })).toBeVisible();
  }
});

it('submits even splitting as one request', async () => {
  const user = userEvent.setup();
  const onConfirm = openSplit();
  await user.click(screen.getByRole('tab', { name: 'floor.split_even' }));
  await user.clear(screen.getByRole('spinbutton', { name: 'floor.split_count' }));
  await user.type(screen.getByRole('spinbutton', { name: 'floor.split_count' }), '3');
  await user.click(screen.getByRole('button', { name: 'bill_split.confirm_split' }));
  expect(onConfirm).toHaveBeenCalledOnce();
  expect(onConfirm).toHaveBeenCalledWith({ mode: 'even', splitCount: 3 });
});
