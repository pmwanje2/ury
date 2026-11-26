import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import TableCard from './TableCard';
import type { Table } from '../lib/table-api';

vi.mock('../i18n', () => ({ t: (key: string) => key }));
vi.mock('./TableActionsMenu', () => ({ default: () => null }));

function show(state: Table['custom_table_state'], occupied = 0) {
  const onMarkClean = vi.fn();
  const onPay = vi.fn();
  render(<TableCard table={{
    name: 'T1', occupied, no_of_seats: 4, restaurant_room: 'Main',
    table_shape: 'Rectangle', branch: 'Outlet', company: 'Company', is_take_away: 0,
    latest_invoice_time: '', custom_table_state: state,
  }} menuOpen={false} onMenuOpenChange={vi.fn()} onMerge={vi.fn()} onUnmerge={vi.fn()}
    onNavigate={vi.fn()} onPreview={vi.fn()} onPrint={vi.fn()} isPrinting={false}
    onMarkClean={onMarkClean} onPay={onPay} />);
  return { onMarkClean, onPay };
}

it('shows a dirty table with a one-tap clean action', async () => {
  const { onMarkClean } = show('Dirty');
  expect(screen.getByText('floor.dirty')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'floor.mark_clean' }));
  expect(onMarkClean).toHaveBeenCalledOnce();
});

it('shows a printed check with a direct pay action', async () => {
  const { onPay } = show('Check Printed', 1);
  expect(screen.getByText('floor.check_printed')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'floor.pay' }));
  expect(onPay).toHaveBeenCalledOnce();
});
