import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CaptainTableCard from '../src/captain/components/CaptainTableCard';
import CaptainTables from '../src/captain/pages/CaptainTables';
import { initI18n } from '../src/i18n';

const boundary = vi.hoisted(() => ({ get: vi.fn(), getDocList: vi.fn() }));
vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: boundary.get }, db: { getDocList: boundary.getDocList },
}));
vi.mock('../src/captain/hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({ context: { user: 'waiter@fixture.invalid' },
    branch: 'Branch A', rooms: [], capabilities: {}, isLoading: false, error: null }),
}));

const table = { name: 'Table 1', occupied: 0, latest_invoice_time: null,
  is_take_away: 0, restaurant_room: 'Dining', table_shape: 'Square' as const, no_of_seats: 4 };
const booking = { scheduled_time: '2026-10-14 19:00:00', party_size: 4,
  customer_name: 'Grace Hopper', server_now: '2026-10-14 18:30:00' };
const method = 'ury.ury.api.ury_table_bookings.get_upcoming_bookings';
let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  await initI18n('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it('shows a compact reservation with site-local time, party size and first name only', async () => {
  await act(async () => root.render(<CaptainTableCard table={table} ownership="free"
    {...{ booking }} onTap={() => {}} />));
  expect(container.textContent).toContain('Reserved 19:00 · 4 · Grace');
  expect(container.textContent).not.toContain('Hopper');
  const badge = [...container.querySelectorAll('div')].find(el => el.textContent === 'Reserved 19:00 · 4 · Grace');
  expect(badge?.className).toContain('bg-secondary');
  expect(badge?.className).toContain('text-secondary-foreground');
});

it('hides a past reservation on an occupied table using server time, not device time', async () => {
  // The device clock disagrees with the site; it must not control visibility.
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-14T01:00:00Z'));
  try {
    await act(async () => root.render(<CaptainTableCard table={{ ...table, occupied: 1 }}
      ownership="mine" {...{ booking: { ...booking, server_now: '2026-10-14 19:01:00' } }} onTap={() => {}} />));
    expect(container.textContent).not.toContain('Reserved');
  } finally {
    vi.restoreAllMocks();
  }
});

it('keeps a future reservation visible on an occupied table', async () => {
  await act(async () => root.render(<CaptainTableCard table={{ ...table, occupied: 1 }}
    ownership="mine" {...{ booking }} onTap={() => {}} />));
  expect(container.textContent).toContain('Reserved 19:00 · 4 · Grace');
});

it('keeps a past reservation visible on a free table without blocking seating', async () => {
  const seat = vi.fn();
  await act(async () => root.render(<CaptainTableCard table={table} ownership="free"
    {...{ booking: { ...booking, server_now: '2026-10-14 19:01:00' } }} onTap={seat} />));
  expect(container.textContent).toContain('Reserved 19:00 · 4 · Grace');
  await act(async () => container.querySelector('button')!.click());
  expect(seat).toHaveBeenCalledOnce();
  expect(container.querySelector('button')!.disabled).toBe(false);
});

it('omits optional party size cleanly and translates the reservation label', async () => {
  await initI18n('fr');
  await act(async () => root.render(<CaptainTableCard table={table} ownership="free"
    {...{ booking: { ...booking, party_size: null } }} onTap={() => {}} />));
  expect(container.textContent).toContain('Réservé 19:00 · Grace');
  expect(container.textContent).not.toMatch(/null|undefined|Hopper/);
});

it('renders no reservation badge when a table has no booking', async () => {
  await act(async () => root.render(<CaptainTableCard table={table} ownership="free" onTap={() => {}} />));
  expect(container.textContent).not.toContain('Reserved');
});

function setupGrid() {
  boundary.getDocList.mockImplementation(async (doctype: string) => {
    if (doctype === 'URY Room') return [{ name: 'Dining', branch: 'Branch A' }];
    if (doctype === 'URY Table') return [table, { ...table, name: 'Table 2' }];
    return [];
  });
  boundary.get.mockResolvedValue({ message: { 'Table 1': booking } });
}

it('bulk-loads bookings on page load and refresh; closed bookings disappear without blocking a tap', async () => {
  setupGrid();
  await act(async () => root.render(<MemoryRouter initialEntries={['/order']}><Routes>
    <Route path="/order" element={<CaptainTables />} />
    <Route path="/order/table/:table" element={<p>Seating allowed</p>} />
  </Routes></MemoryRouter>));
  expect(container.textContent).toContain('Reserved 19:00 · 4 · Grace');
  expect(boundary.get).toHaveBeenCalledExactlyOnceWith(method, { branch: 'Branch A' });
  boundary.get.mockResolvedValue({ message: {} });
  await act(async () => [...container.querySelectorAll('button')].find(el => el.textContent === 'Refresh')!.click());
  expect(boundary.get).toHaveBeenCalledTimes(2);
  expect(container.textContent).not.toContain('Reserved');
  await act(async () => [...container.querySelectorAll('button')].find(el => el.textContent?.includes('Table 1'))!.click());
  expect(container.textContent).toContain('Seating allowed');
});

it('booking read failure is non-fatal and clears a stale badge on refresh', async () => {
  setupGrid();
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  try {
    await act(async () => root.render(<MemoryRouter><CaptainTables /></MemoryRouter>));
    expect(container.textContent).toContain('Reserved 19:00 · 4 · Grace');
    boundary.get.mockRejectedValue(new Error('Read denied'));
    await act(async () => [...container.querySelectorAll('button')].find(el => el.textContent === 'Refresh')!.click());
    expect(container.textContent).not.toContain('Reserved');
    expect(container.textContent).toContain('Table 1');
    expect(container.textContent).toContain('Free');
  } finally {
    error.mockRestore();
  }
});
