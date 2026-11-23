import { fireEvent, render, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import TableReservationTimeline from '../src/components/TableReservationTimeline';
import type { Table, TableReservation } from '../src/lib/table-api';
import { initI18n } from '../src/i18n';

// The reservation book's timeline is a table-by-time grid: one row per table
// (empty tables included, so a host can see where a walk-in fits), with each
// booking in its own table's row.
const table = (name: string): Table => ({
  name, occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining',
  table_shape: 'Square', no_of_seats: 4,
});
const booking = (name: string, reservedTable: string, guest: string): TableReservation => ({
  name, branch: 'Branch A', reserved_table: reservedTable, customer: guest, customer_name: guest,
  no_of_pax: 2, reserved_at: '2026-11-23 19:00:00', status: 'Confirmed', duration_minutes: 90,
});
const tables = ['Table 1', 'Table 2', 'Table 3'].map(table);
const reservations = [booking('TR-1', 'Table 1', 'Grace'), booking('TR-2', 'Table 2', 'Ada')];

beforeEach(async () => {
  await initI18n('en');
  Element.prototype.scrollTo = vi.fn();
});

it('lays bookings out one row per table and opens the one that is clicked', () => {
  const onSelect = vi.fn();
  const view = render(
    <TableReservationTimeline tables={tables} reservations={reservations}
      selectedDate={new Date(2026, 10, 23)} onSelectReservation={onSelect} />,
  );
  const rows = view.container.querySelectorAll('[role="row"]');
  expect([...rows].map(row => row.getAttribute('aria-label'))).toEqual(['Table 1', 'Table 2', 'Table 3']);
  const row = (name: string) => view.container.querySelector(`[role="row"][aria-label="${name}"]`) as HTMLElement;
  expect(within(row('Table 1')).getByText('Grace')).toBeInTheDocument();
  expect(within(row('Table 1')).queryByText('Ada')).toBeNull();
  expect(within(row('Table 2')).getByText('Ada')).toBeInTheDocument();
  expect(row('Table 3').textContent).not.toMatch(/Grace|Ada/);
  fireEvent.click(within(row('Table 1')).getByText('Grace'));
  expect(onSelect).toHaveBeenCalledWith(reservations[0]);

  // Without the floor's tables the timeline keeps its free time-axis layout.
  view.rerender(<TableReservationTimeline reservations={reservations} selectedDate={new Date(2026, 10, 23)} />);
  expect(view.container.querySelectorAll('[role="row"]')).toHaveLength(0);
  expect(view.container.textContent).toMatch(/Grace/);
  expect(view.container.textContent).toMatch(/Ada/);
});
