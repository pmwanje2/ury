import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import Reservations from '../src/pages/Reservations';
import { initI18n } from '../src/i18n';

// The reservation book: whoever greets the party seats it (Seated is its own
// state, between Confirmed and Completed, and seating opens the table's
// order); a cancellation records why; the timeline shows bookings per table.
const boundary = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), getDocList: vi.fn(),
  setSelectedTable: vi.fn(), setSelectedOrderType: vi.fn(),
  user: { name: 'captain@fixture.invalid', roles: ['URY Captain'] } as { name: string; roles: string[] },
  posProfile: { branch: 'Branch A', role_allowed_for_billing: [{ role: 'URY Cashier' }], transfer_role_permissions: [] },
  reservations: [] as Array<Record<string, unknown>>,
}));
// table-api reads @ury/core both statically and through dynamic imports, and a
// later dynamic import can resolve the unmocked module; patching the shared
// transport objects makes every path reach the same fakes.
vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  Object.assign(actual.call, { get: boundary.get, post: boundary.post });
  Object.assign(actual.db, { getDocList: boundary.getDocList });
  return { ...actual, call: actual.call, db: actual.db };
});
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: boundary.posProfile, searchQuery: '',
    setSelectedTable: boundary.setSelectedTable, setSelectedOrderType: boundary.setSelectedOrderType,
  }),
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: Function) => select({ user: boundary.user }) }));

const RESERVATION = 'ury.ury.api.table_reservation.';
const today = new Date();
const pad = (n: number) => String(n).padStart(2, '0');
const DAY = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
const booking = (name: string, table: string, guest: string, status: string, clock = '19:00') => ({
  name, branch: 'Branch A', reserved_table: table, customer: guest, customer_name: guest,
  customer_phone: '+256772123456', no_of_pax: 4, reserved_at: `${DAY} ${clock}:00`, status,
  is_lock_window_active: false, duration_minutes: 90,
});

beforeEach(async () => {
  vi.resetAllMocks();
  await initI18n('en');
  Element.prototype.scrollTo = vi.fn();
  boundary.user = { name: 'captain@fixture.invalid', roles: ['URY Captain'] };
  boundary.reservations = [booking('TR-1', 'Table 1', 'Grace', 'Confirmed')];
  boundary.getDocList.mockImplementation(async (doctype: string) => {
    if (doctype === 'URY Room') return [{ name: 'Dining', branch: 'Branch A' }];
    if (doctype === 'URY Table') return ['Table 1', 'Table 2'].map(name => ({
      name, occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining',
      table_shape: 'Square', no_of_seats: 4,
    }));
    throw new Error(`Unexpected doctype: ${doctype}`);
  });
  boundary.get.mockImplementation(async (endpoint: string) => {
    if (endpoint === `${RESERVATION}get_branch_reservation_settings`) {
      return { message: { enable_reservation: 1, buffer_time: 30, grace_period: 15 } };
    }
    if (endpoint === `${RESERVATION}get_active_reservations`) return { message: boundary.reservations };
    if (endpoint === `${RESERVATION}get_available_tables_for_reservation`) return { message: [] };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  boundary.post.mockResolvedValue({ message: true });
});

async function openBook() {
  render(
    <MemoryRouter initialEntries={['/reservations']}>
      <Routes>
        <Route path="/reservations" element={<Reservations />} />
        <Route path="/register" element={<div>REGISTER</div>} />
        <Route path="/tables" element={<div>FLOOR</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByText('Grace').length).toBeGreaterThan(0));
}

it('a captain seats a confirmed booking and lands on its table order', async () => {
  await openBook();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /seat guest/i })); });
  const dialog = screen.getByRole('dialog');
  expect(dialog.textContent).toMatch(/Grace/);
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /seat guest/i })); });
  await waitFor(() => expect(screen.getByText('REGISTER')).toBeInTheDocument());
  expect(boundary.post).toHaveBeenCalledWith(`${RESERVATION}update_reservation_status`,
    expect.objectContaining({ reservation_name: 'TR-1', status: 'Seated' }));
  expect(boundary.setSelectedOrderType).toHaveBeenCalledWith('Dine In');
  expect(boundary.setSelectedTable).toHaveBeenCalledWith('Table 1', 'Dining');
});

it('Seated has its own filter and badge', async () => {
  boundary.reservations = [booking('TR-1', 'Table 1', 'Grace', 'Confirmed'),
    booking('TR-2', 'Table 2', 'Ada', 'Seated', '18:30')];
  await openBook();
  const filter = screen.getByRole('combobox', { name: /filter by status/i }) as HTMLSelectElement;
  expect([...filter.options].map(option => option.value)).toContain('Seated');
  await act(async () => { fireEvent.change(filter, { target: { value: 'Seated' } }); });
  await waitFor(() => expect(screen.queryByText('Grace')).toBeNull());
  expect(screen.getByText('Ada')).toBeInTheDocument();
  expect(screen.getAllByText('Seated').some(el => el.tagName !== 'OPTION')).toBe(true);
});

it('a cancellation cannot be sent without a reason and sends the one typed', async () => {
  boundary.user = { name: 'Administrator', roles: ['System Manager'] };
  await openBook();
  const menu = [...document.querySelectorAll('button')]
    .filter(button => !button.textContent?.trim() && !button.getAttribute('aria-label'));
  expect(menu).toHaveLength(1);
  await act(async () => { fireEvent.click(menu[0]); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /cancel reservation/i })); });
  const dialog = screen.getByRole('dialog');
  const confirm = within(dialog).getAllByRole('button', { name: /cancel reservation/i }).at(-1)!;
  expect(confirm).toBeDisabled();
  fireEvent.change(within(dialog).getByRole('textbox', { name: /cancellation reason/i }),
    { target: { value: 'Guest called' } });
  expect(confirm).toBeEnabled();
  await act(async () => { fireEvent.click(confirm); });
  await waitFor(() => expect(boundary.post).toHaveBeenCalledWith(`${RESERVATION}update_reservation_status`,
    expect.objectContaining({ reservation_name: 'TR-1', status: 'Cancelled', cancel_reason: 'Guest called' })));
});

it('the timeline shows bookings per table and opens one on click', async () => {
  await openBook();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /timeline view/i })); });
  const row = await waitFor(() => {
    const found = document.querySelector('[role="row"][aria-label="Table 1"]') as HTMLElement | null;
    expect(found).not.toBeNull();
    return found!;
  });
  expect(document.querySelector('[role="row"][aria-label="Table 2"]')).not.toBeNull();
  await act(async () => { fireEvent.click(within(row).getByText('Grace')); });
  await waitFor(() => expect(within(screen.getByRole('dialog')).getAllByText(/edit reservation/i).length)
    .toBeGreaterThan(0));
});
