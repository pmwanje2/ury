import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import Reservations from '../src/pages/Reservations';
import TableReservationTimeline from '../src/components/TableReservationTimeline';
import { getBranchReservationSettings, type TableReservation } from '../src/lib/table-api';
import { initI18n } from '../src/i18n';

// Reservation timing is Branch configuration (Simphony's Minimum Reservation
// Notice and Target Turn Time). The book reads it from the server and never
// substitutes timings of its own.
const boundary = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), getDocList: vi.fn(),
  setSelectedTable: vi.fn(), setSelectedOrderType: vi.fn(),
  settings: {} as Record<string, unknown> | null,
}));
// table-api reads @ury/core both statically and through dynamic imports;
// patching the shared transport objects makes every path reach the same fakes.
vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  Object.assign(actual.call, { get: boundary.get, post: boundary.post });
  Object.assign(actual.db, { getDocList: boundary.getDocList });
  return { ...actual, call: actual.call, db: actual.db };
});
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: { branch: 'Branch A', role_allowed_for_billing: [], transfer_role_permissions: [] },
    searchQuery: '', setSelectedTable: boundary.setSelectedTable, setSelectedOrderType: boundary.setSelectedOrderType,
  }),
}));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (select: Function) => select({ user: { name: 'Administrator', roles: ['System Manager'] } }),
}));

const RESERVATION = 'ury.ury.api.table_reservation.';
const TABLE_1 = {
  name: 'Table 1', occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining',
  table_shape: 'Square', no_of_seats: 4,
};
const BOOKING: TableReservation = {
  name: 'TR-1', branch: 'Branch A', reserved_table: 'Table 1', customer: 'CUST-1', customer_name: 'Grace',
  customer_phone: '+256772123456', no_of_pax: 4, reserved_at: '2026-11-23 19:00:00', status: 'Confirmed',
  is_lock_window_active: false, duration_minutes: 90,
};

beforeEach(async () => {
  vi.resetAllMocks();
  // The site clock reads 12:00 on the booking's day; only Date is faked.
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 10, 23, 12, 0, 0));
  await initI18n('en');
  Element.prototype.scrollTo = vi.fn();
  boundary.settings = { enable_reservation: 1, buffer_time: 30, grace_period: 15, min_reservation_notice: -1 };
  boundary.getDocList.mockImplementation(async (doctype: string) => {
    if (doctype === 'URY Room') return [{ name: 'Dining', branch: 'Branch A' }];
    if (doctype === 'URY Table') return [TABLE_1];
    throw new Error(`Unexpected doctype: ${doctype}`);
  });
  boundary.get.mockImplementation(async (endpoint: string) => {
    if (endpoint === `${RESERVATION}get_branch_reservation_settings`) return { message: boundary.settings };
    if (endpoint === `${RESERVATION}get_active_reservations`) return { message: [BOOKING] };
    if (endpoint === `${RESERVATION}get_available_tables_for_reservation`) return { message: [TABLE_1] };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  boundary.post.mockResolvedValue({ message: true });
  vi.spyOn(showToast, 'error').mockImplementation(() => 'toast');
  vi.spyOn(showToast, 'success').mockImplementation(() => 'toast');
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function openEdit() {
  render(
    <MemoryRouter initialEntries={['/reservations']}>
      <Routes>
        <Route path="/reservations" element={<Reservations />} />
        <Route path="/tables" element={<div>FLOOR</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByText('Grace').length).toBeGreaterThan(0));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /timeline view/i })); });
  const row = await waitFor(() => {
    const found = document.querySelector('[role="row"][aria-label="Table 1"]') as HTMLElement | null;
    expect(found).not.toBeNull();
    return found!;
  });
  await act(async () => { fireEvent.click(within(row).getByText('Grace')); });
  const dialog = await waitFor(() => screen.getByRole('dialog'));
  await waitFor(() => expect(boundary.get).toHaveBeenCalledWith(
    `${RESERVATION}get_available_tables_for_reservation`, expect.anything()));
  return dialog;
}

const saved = () => boundary.post.mock.calls.some(([endpoint]) => endpoint === `${RESERVATION}update_table_reservation`);

async function saveAt(dialog: HTMLElement, clock?: string) {
  if (clock) {
    await act(async () => {
      fireEvent.change(within(dialog).getByDisplayValue('19:00'), { target: { value: clock } });
    });
  }
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i })); });
}

it("refuses moving a booking inside the branch's minimum notice, with the notice", async () => {
  boundary.settings = { ...boundary.settings, min_reservation_notice: 480 };
  const dialog = await openEdit();
  await saveAt(dialog, '19:30');
  await waitFor(() => expect(dialog.textContent).toMatch(/at least 480 minutes/));
  expect(saved()).toBe(false);
});

it("saves moving a booking outside the branch's minimum notice", async () => {
  boundary.settings = { ...boundary.settings, min_reservation_notice: 360 };
  const dialog = await openEdit();
  await saveAt(dialog, '19:30');
  await waitFor(() => expect(saved()).toBe(true));
});

it('saves an edit that keeps the time, even inside the notice', async () => {
  // The notice applies to the booking time (Simphony), not to notes, covers or the phone.
  boundary.settings = { ...boundary.settings, min_reservation_notice: 480 };
  const dialog = await openEdit();
  await saveAt(dialog);
  await waitFor(() => expect(saved()).toBe(true));
  expect(dialog.textContent).not.toMatch(/at least 480 minutes/);
});

it('keeps the booking table selected when a walk-in omits it on a non-time edit, and clears it only if the time moves', async () => {
  // Probe 6 (P1-3): when a walk-in runs into the booking buffer, getAvailableTablesForReservation
  // omits Table 1. Opening the edit dialog must keep Table 1 selected for notes/covers/phone edits,
  // and clear it only if the staff changes the booking time.
  const TABLE_2 = { ...TABLE_1, name: 'Table 2' };
  boundary.get.mockImplementation(async (endpoint: string) => {
    if (endpoint === `${RESERVATION}get_branch_reservation_settings`) return { message: boundary.settings };
    if (endpoint === `${RESERVATION}get_active_reservations`) return { message: [BOOKING] };
    if (endpoint === `${RESERVATION}get_available_tables_for_reservation`) return { message: [TABLE_2] };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  const dialog = await openEdit();
  const tableInput = dialog.querySelector('#edit-reservation-table') as HTMLInputElement;
  expect(tableInput.value).toContain('Table 1');
  const saveButton = within(dialog).getByRole('button', { name: /save changes/i });
  expect(saveButton).not.toBeDisabled();

  // Changing notes keeps Table 1 and submits it.
  const textarea = dialog.querySelector('textarea') as HTMLTextAreaElement;
  await act(async () => { fireEvent.change(textarea, { target: { value: 'Needs high chair' } }); });
  await act(async () => { fireEvent.click(saveButton); });
  await waitFor(() => expect(saved()).toBe(true));
  expect(boundary.post).toHaveBeenCalledWith(
    `${RESERVATION}update_table_reservation`,
    expect.objectContaining({ reservation_name: 'TR-1', table: 'Table 1', notes: 'Needs high chair' }),
  );
});

it('clears the booking table when the edit moves the time and the table is unavailable for the new slot', async () => {
  const TABLE_2 = { ...TABLE_1, name: 'Table 2' };
  boundary.get.mockImplementation(async (endpoint: string) => {
    if (endpoint === `${RESERVATION}get_branch_reservation_settings`) return { message: boundary.settings };
    if (endpoint === `${RESERVATION}get_active_reservations`) return { message: [BOOKING] };
    if (endpoint === `${RESERVATION}get_available_tables_for_reservation`) return { message: [TABLE_2] };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  const dialog = await openEdit();
  const tableInput = dialog.querySelector('#edit-reservation-table') as HTMLInputElement;
  expect(tableInput.value).toContain('Table 1');

  await act(async () => {
    fireEvent.change(within(dialog).getByDisplayValue('19:00'), { target: { value: '19:30' } });
  });
  await waitFor(() => expect(tableInput.value).toBe(''));
  expect(within(dialog).getByRole('button', { name: /save changes/i })).toBeDisabled();
});

it('a negative notice accepts a time that many minutes back, and no further', async () => {
  boundary.settings = { ...boundary.settings, min_reservation_notice: -5 };
  const dialog = await openEdit();
  await saveAt(dialog, '11:54');
  await waitFor(() => expect(dialog.textContent).toMatch(/cannot be in the past/));
  expect(saved()).toBe(false);
  await act(async () => {
    fireEvent.change(within(dialog).getByDisplayValue('11:54'), { target: { value: '11:56' } });
  });
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /save changes/i })); });
  await waitFor(() => expect(saved()).toBe(true));
});

it('reads no invented timings when the server sends no settings', async () => {
  boundary.settings = null;
  await expect(getBranchReservationSettings('Branch A')).resolves.toBeNull();
});

it("shows each booking for the server's duration and invents none", () => {
  const view = render(
    <TableReservationTimeline tables={[TABLE_1]} reservations={[{ ...BOOKING, duration_minutes: 45 }]}
      selectedDate={new Date(2026, 10, 23)} />,
  );
  expect(view.container.textContent).toMatch(/19:00 - 19:45/);
  view.rerender(
    <TableReservationTimeline tables={[TABLE_1]} reservations={[{ ...BOOKING, duration_minutes: 0 }]}
      selectedDate={new Date(2026, 10, 23)} />,
  );
  expect(view.container.textContent).toMatch(/19:00 - 19:00/);
  // The hour axis prints 20:00 anyway; the booking's own span must not.
  expect(view.container.textContent).not.toMatch(/19:00 - 20:00/);
});
