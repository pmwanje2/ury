import { afterEach, expect, it, vi } from 'vitest';
import { isReservationLockWindowActive } from '../src/lib/table-utils';
import type { TableReservation } from '../src/lib/table-api';

// The server decides whether a booking holds its table: it runs on the site
// clock, which a tablet with a wrong clock or time zone must not overrule.
// The browser estimate is only a fallback for rows that carry no server flag,
// and like the server it ends at the grace end, when an unclaimed table is released.
const booking = (overrides: Partial<TableReservation> = {}): TableReservation => ({
  name: 'TR-1', branch: 'Branch A', reserved_table: 'Table 1', customer: 'CUST-1',
  reserved_at: '2026-11-23 19:00:00', status: 'Confirmed', buffer_minutes: 30, grace_minutes: 15,
  ...overrides,
});

const browserClock = (hours: number, minutes: number) => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 10, 23, hours, minutes));
};

afterEach(() => vi.useRealTimers());

it('the server lock flag wins over the browser clock in both directions', () => {
  browserClock(19, 20);
  expect(isReservationLockWindowActive(booking({ is_lock_window_active: false }))).toBe(false);
  browserClock(12, 0);
  expect(isReservationLockWindowActive(booking({ is_lock_window_active: true }))).toBe(true);
});

it('without a server flag the browser estimate runs from the buffer to the grace end', () => {
  const cases: Array<[number, number, boolean]> = [
    [18, 29, false], [18, 30, true], [19, 14, true], [19, 15, false], [21, 0, false],
  ];
  for (const [hours, minutes, locked] of cases) {
    browserClock(hours, minutes);
    expect({ at: `${hours}:${minutes}`, locked: isReservationLockWindowActive(booking()) })
      .toEqual({ at: `${hours}:${minutes}`, locked });
  }
});
