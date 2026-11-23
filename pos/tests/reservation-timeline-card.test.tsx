import { render, within } from '@testing-library/react';
import { beforeAll, beforeEach, expect, it, vi } from 'vitest';
import TableReservationTimeline from '../src/components/TableReservationTimeline';
import type { Table, TableReservation } from '../src/lib/table-api';
import { initI18n } from '../src/i18n';
import { installTailwindUtilities, truncationStyle, TRUNCATES } from './helpers/tailwind-css';

// At a 45-60 minute turn a booking card is 120px wide, about 104px inside. Its
// status must show whole. In the per-table timeline the row already names the
// table and the card's last line shows its times, so the top line repeats
// neither; in the free layout the table name yields to the status instead.
const table = (name: string): Table => ({
  name, occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining',
  table_shape: 'Square', no_of_seats: 4,
});
const booking: TableReservation = {
  name: 'TR-1', branch: 'Branch A', reserved_table: 'Table 1', customer: 'Grace', customer_name: 'Grace',
  customer_phone: '+256772123456', no_of_pax: 2, reserved_at: '2026-11-23 19:00:00', status: 'Confirmed',
  duration_minutes: 45,
};

beforeAll(async () => {
  await installTailwindUtilities([
    'src/components/TableReservationTimeline.tsx',
    '../packages/ui/src/components/badge.tsx',
  ]);
});

beforeEach(async () => {
  await initI18n('en');
  Element.prototype.scrollTo = vi.fn();
});

/** The booking card: the positioned block that holds the guest's name. */
function cardOf(container: HTMLElement) {
  let node: HTMLElement | null = within(container).getByText('Grace');
  while (node && !node.style.width) node = node.parentElement;
  expect(node).not.toBeNull();
  return node!;
}

it("a card in a table's row shows its status whole, without repeating the table or its start", () => {
  const view = render(
    <TableReservationTimeline tables={[table('Table 1')]} reservations={[booking]}
      selectedDate={new Date(2026, 10, 23)} />,
  );
  const card = cardOf(view.container);
  expect(card.style.width).toBe('120px');
  expect(within(card).queryByText('Table 1')).toBeNull();
  expect(card.textContent?.match(/19:00/g)).toHaveLength(1);
  expect(card.textContent).toMatch(/19:00 - 19:45/);
  const status = within(card).getByText('Confirmed');
  expect(truncationStyle(status).textOverflow).not.toBe('ellipsis');
});

it('in the free layout the table name yields to the status', () => {
  const view = render(
    <TableReservationTimeline reservations={[booking]} selectedDate={new Date(2026, 10, 23)} />,
  );
  const card = cardOf(view.container);
  expect(truncationStyle(within(card).getByText('Table 1'))).toEqual(TRUNCATES);
  expect(truncationStyle(within(card).getByText('Confirmed')).textOverflow).not.toBe('ellipsis');
  expect(card.textContent?.match(/19:00/g)).toHaveLength(1);
});
