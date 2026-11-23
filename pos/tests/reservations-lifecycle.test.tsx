import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
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
  vi.spyOn(showToast, 'error').mockImplementation(() => 'toast');
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
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: true }];
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

const actionMenus = () => [...document.querySelectorAll('button')]
  .filter(button => !button.textContent?.trim() && !button.getAttribute('aria-label'));

it('offers Cancel on exactly the bookings the server says this user may cancel, whatever the roles', async () => {
  // The server evaluates the native cancel_reservation permission per booking (owner and
  // User Permission included); the POS never guesses it from role names.
  boundary.user = { name: 'Administrator', roles: ['System Manager'] };
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: false },
    { ...booking('TR-2', 'Table 2', 'Ada', 'Confirmed', '20:00'), can_cancel: true }];
  await openBook();
  const menus = actionMenus();
  expect(menus).toHaveLength(2);
  await act(async () => { fireEvent.click(menus[0]); });
  expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /cancel reservation/i })).toBeNull();
  await act(async () => { fireEvent.click(menus[1]); });
  expect(screen.getByRole('button', { name: /cancel reservation/i })).toBeInTheDocument();
});

it('offers Mark as No Show on exactly the bookings the server says this user may mark', async () => {
  // Simphony 32007 Mark as No Show is its own privilege, apart from 32012 Cancel Reservation
  // Entry: the server evaluates the native mark_no_show permission per booking.
  boundary.user = { name: 'Administrator', roles: ['System Manager'] };
  boundary.reservations = [
    { ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: true, can_mark_no_show: false },
    { ...booking('TR-2', 'Table 2', 'Ada', 'Confirmed', '20:00'), can_cancel: false, can_mark_no_show: true },
  ];
  await openBook();
  const menus = actionMenus();
  expect(menus).toHaveLength(2);
  await act(async () => { fireEvent.click(menus[0]); });
  expect(screen.getByRole('button', { name: /cancel reservation/i })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /mark as no show/i })).toBeNull();
  await act(async () => { fireEvent.click(menus[1]); });
  expect(screen.queryByRole('button', { name: /cancel reservation/i })).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /mark as no show/i })); });
  await waitFor(() => expect(boundary.post).toHaveBeenCalledWith(`${RESERVATION}update_reservation_status`,
    expect.objectContaining({ reservation_name: 'TR-2', status: 'No Show' })));
});

it('a captain the server does not let mark No Show never sees it', async () => {
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: false }];
  await openBook();
  await act(async () => { fireEvent.click(actionMenus()[0]); });
  expect(screen.getByRole('button', { name: /edit/i })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /mark as no show/i })).toBeNull();
});

it('a refused No Show is shown to the host', async () => {
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_mark_no_show: true }];
  boundary.post.mockRejectedValue(new Error('Not permitted to change reservation status to No Show.'));
  await openBook();
  await act(async () => { fireEvent.click(actionMenus()[0]); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /mark as no show/i })); });
  await waitFor(() => expect(showToast.error).toHaveBeenCalledWith('Not permitted to change reservation status to No Show.'));
});

it('a hotel desk the server lets cancel sees Cancel without any till role', async () => {
  boundary.user = { name: 'desk@fixture.invalid', roles: ['Front Desk'] };
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: true }];
  await openBook();
  await act(async () => { fireEvent.click(actionMenus()[0]); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /cancel reservation/i })); });
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByRole('textbox', { name: /cancellation reason/i }),
    { target: { value: 'Guest called the hotel' } });
  await act(async () => { fireEvent.click(within(dialog).getAllByRole('button', { name: /cancel reservation/i }).at(-1)!); });
  await waitFor(() => expect(boundary.post).toHaveBeenCalledWith(`${RESERVATION}update_reservation_status`,
    expect.objectContaining({ reservation_name: 'TR-1', status: 'Cancelled', cancel_reason: 'Guest called the hotel' })));
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
  // The booking opens in the edit dialog, titled with its table.
  const dialog = await waitFor(() => screen.getByRole('dialog'));
  expect(within(dialog).getByRole('button', { name: /save changes/i })).toBeInTheDocument();
  expect(within(dialog).getByRole('heading', { name: 'Table 1' })).toBeInTheDocument();
});

// The book opens on Confirmed; a test about another status switches the filter first.
async function openBookOn(status: string, guest: string) {
  render(
    <MemoryRouter initialEntries={['/reservations']}>
      <Routes>
        <Route path="/reservations" element={<Reservations />} />
        <Route path="/register" element={<div>REGISTER</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(boundary.get).toHaveBeenCalledWith(
    `${RESERVATION}get_active_reservations`, expect.anything()));
  await showStatus(status);
  await waitFor(() => expect(screen.getAllByText(guest).length).toBeGreaterThan(0));
}

async function showStatus(value: string) {
  const filter = screen.getByRole('combobox', { name: /filter by status/i });
  await act(async () => { fireEvent.change(filter, { target: { value } }); });
}

it('a refused seat is shown to the host, who stays on the book', async () => {
  boundary.post.mockRejectedValue(new Error('Table Table 1 has an open check.'));
  await openBook();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /seat guest/i })); });
  const dialog = screen.getByRole('dialog');
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /seat guest/i })); });
  await waitFor(() => expect(showToast.error).toHaveBeenCalledWith('Table Table 1 has an open check.'));
  expect(screen.queryByText('REGISTER')).toBeNull();
  expect(boundary.setSelectedTable).not.toHaveBeenCalled();
});

it('a refused cancellation is shown to the host', async () => {
  boundary.user = { name: 'Administrator', roles: ['System Manager'] };
  // The server reports which bookings this user may cancel (merged with #140 R2).
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: true }];
  boundary.post.mockRejectedValue(new Error('Not permitted to change reservation status to Cancelled.'));
  await openBook();
  const menu = [...document.querySelectorAll('button')]
    .filter(button => !button.textContent?.trim() && !button.getAttribute('aria-label'));
  await act(async () => { fireEvent.click(menu[0]); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /cancel reservation/i })); });
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByRole('textbox', { name: /cancellation reason/i }),
    { target: { value: 'Guest called' } });
  await act(async () => {
    fireEvent.click(within(dialog).getAllByRole('button', { name: /cancel reservation/i }).at(-1)!);
  });
  await waitFor(() => expect(showToast.error).toHaveBeenCalledWith('Not permitted to change reservation status to Cancelled.'));
});

it('the cancellation reason is capped at the stored field length', async () => {
  boundary.user = { name: 'Administrator', roles: ['System Manager'] };
  // The server reports which bookings this user may cancel (merged with #140 R2).
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Confirmed'), can_cancel: true }];
  await openBook();
  const menu = [...document.querySelectorAll('button')]
    .filter(button => !button.textContent?.trim() && !button.getAttribute('aria-label'));
  await act(async () => { fireEvent.click(menu[0]); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /cancel reservation/i })); });
  const reason = within(screen.getByRole('dialog')).getByRole('textbox', { name: /cancellation reason/i });
  expect(reason).toHaveAttribute('maxLength', '140');
});

it('only a confirmed booking offers Seat guest', async () => {
  boundary.reservations = [booking('TR-1', 'Table 1', 'Grace', 'Active'),
    booking('TR-2', 'Table 2', 'Ada', 'Requested', '20:00')];
  await openBookOn('All', 'Ada');
  expect(screen.getByText('Grace')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /seat guest/i })).toBeNull();
});

it('a party seated by mistake is unseated back to the book', async () => {
  boundary.reservations = [booking('TR-1', 'Table 1', 'Grace', 'Seated')];
  await openBookOn('Seated', 'Grace');
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^unseat$/i })); });
  await waitFor(() => expect(boundary.post).toHaveBeenCalledWith(`${RESERVATION}update_reservation_status`,
    expect.objectContaining({ reservation_name: 'TR-1', status: 'Confirmed' })));
});

it('a refused unseat is shown to the host', async () => {
  boundary.reservations = [booking('TR-1', 'Table 1', 'Grace', 'Seated')];
  boundary.post.mockRejectedValue(new Error('Table Table 1 has an open check; the party has ordered.'));
  await openBookOn('Seated', 'Grace');
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^unseat$/i })); });
  await waitFor(() => expect(showToast.error).toHaveBeenCalledWith('Table Table 1 has an open check; the party has ordered.'));
});

it('a cancelled booking shows why it was cancelled', async () => {
  boundary.reservations = [{ ...booking('TR-1', 'Table 1', 'Grace', 'Cancelled'), cancel_reason: 'Guest called' }];
  await openBookOn('Cancelled', 'Grace');
  expect(screen.getByText(/Guest called/)).toBeInTheDocument();
});

it('the timeline opens only editable bookings', async () => {
  boundary.reservations = [booking('TR-1', 'Table 1', 'Grace', 'Confirmed'),
    booking('TR-2', 'Table 2', 'Ada', 'Completed', '12:30')];
  await openBookOn('All', 'Ada');
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /timeline view/i })); });
  const row = await waitFor(() => {
    const found = document.querySelector('[role="row"][aria-label="Table 2"]') as HTMLElement | null;
    expect(found).not.toBeNull();
    return found!;
  });
  await act(async () => { fireEvent.click(within(row).getByText('Ada')); });
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('the timeline follows the table filter', async () => {
  await openBook();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /timeline view/i })); });
  await waitFor(() => expect(document.querySelector('[role="row"][aria-label="Table 2"]')).not.toBeNull());
  await act(async () => {
    fireEvent.change(document.getElementById('table-filter')!, { target: { value: 'Table 1' } });
  });
  await waitFor(() => expect(document.querySelector('[role="row"][aria-label="Table 2"]')).toBeNull());
  expect(document.querySelector('[role="row"][aria-label="Table 1"]')).not.toBeNull();
});
