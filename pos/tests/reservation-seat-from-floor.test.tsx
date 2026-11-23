import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import TableView from '../src/pages/Table';
import { initI18n } from '../src/i18n';

// A host who taps a table held by an arriving booking sees the booking and
// can seat that party there (which opens the table's order), or send the
// walk-in elsewhere. A held table is never just refused with a toast.
const boundary = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), getDocList: vi.fn(),
  setSelectedTable: vi.fn(), setSelectedOrderType: vi.fn(),
  user: { name: 'captain@fixture.invalid', roles: ['URY Captain'] },
  posProfile: { branch: 'Branch A', role_allowed_for_billing: [{ role: 'URY Cashier' }], transfer_role_permissions: [] },
}));
// table-api reads @ury/core both statically and through dynamic imports, and a
// later dynamic import can resolve the unmocked module; patching the shared
// transport objects makes every path reach the same fakes.
vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  Object.assign(actual.call, { get: boundary.get, post: boundary.post });
  Object.assign(actual.db, { getDocList: boundary.getDocList, getCount: vi.fn(async () => 1) });
  return { ...actual, call: actual.call, db: actual.db };
});
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    posProfile: boundary.posProfile,
    setSelectedTable: boundary.setSelectedTable,
    setSelectedOrderType: boundary.setSelectedOrderType,
  }),
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: Function) => select({ user: boundary.user }) }));

const RESERVATION = 'ury.ury.api.table_reservation.';
const today = new Date();
const pad = (n: number) => String(n).padStart(2, '0');
const RESERVED_AT = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())} 19:00:00`;
const HELD = {
  name: 'TR-1', branch: 'Branch A', reserved_table: 'Table 1', customer: 'CUST-1', customer_name: 'Grace',
  customer_phone: '+256772123456', no_of_pax: 4, reserved_at: RESERVED_AT, status: 'Confirmed',
  is_lock_window_active: true, buffer_minutes: 30, grace_minutes: 15, duration_minutes: 90,
};

beforeEach(async () => {
  vi.resetAllMocks();
  sessionStorage.clear();
  await initI18n('en');
  vi.spyOn(showToast, 'error').mockImplementation(() => 'toast');
  vi.spyOn(showToast, 'warning').mockImplementation(() => 'toast');
  boundary.getDocList.mockImplementation(async (doctype: string) => {
    if (doctype === 'URY Room') return [{ name: 'Dining', branch: 'Branch A' }];
    if (doctype === 'URY Table') return [{
      name: 'Table 1', occupied: 0, latest_invoice_time: null, is_take_away: 0,
      restaurant_room: 'Dining', table_shape: 'Square', no_of_seats: 4,
    }];
    throw new Error(`Unexpected doctype: ${doctype}`);
  });
  boundary.get.mockImplementation(async (endpoint: string) => {
    if (endpoint === `${RESERVATION}get_branch_reservation_settings`) {
      return { message: { enable_reservation: 1, buffer_time: 30, grace_period: 15 } };
    }
    if (endpoint === `${RESERVATION}get_active_reservations`) return { message: [HELD] };
    if (endpoint === `${RESERVATION}check_table_reservation`) return { message: HELD };
    if (endpoint.endsWith('.get_order_invoice')) return { message: null };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  boundary.post.mockResolvedValue({ message: true });
});

afterEach(() => vi.restoreAllMocks());

async function tapHeldTable() {
  render(
    <MemoryRouter initialEntries={['/tables']}>
      <Routes>
        <Route path="/tables" element={<TableView />} />
        <Route path="/register" element={<div>REGISTER</div>} />
      </Routes>
    </MemoryRouter>,
  );
  const card = await waitFor(() => {
    const found = [...document.querySelectorAll<HTMLElement>('[role="button"]')]
      .find(el => el.textContent?.includes('Table 1') && el.textContent?.includes('Reserved'));
    expect(found).toBeDefined();
    return found!;
  });
  await act(async () => { fireEvent.click(card); });
  return waitFor(() => screen.getByRole('dialog'));
}

it('tapping a held table shows the booking and seats the party into its order', async () => {
  const dialog = await tapHeldTable();
  expect(dialog.textContent).toMatch(/Grace/);
  expect(dialog.textContent).toMatch(/4 guests/);
  expect(dialog.textContent).toMatch(/7:00/);
  expect(within(dialog).getByRole('button', { name: /choose another table/i })).toBeInTheDocument();
  expect(showToast.error).not.toHaveBeenCalled();

  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /seat guest/i })); });
  await waitFor(() => expect(screen.getByText('REGISTER')).toBeInTheDocument());
  expect(boundary.post).toHaveBeenCalledWith(`${RESERVATION}update_reservation_status`,
    expect.objectContaining({ reservation_name: 'TR-1', status: 'Seated' }));
  expect(boundary.setSelectedOrderType).toHaveBeenCalledWith('Dine In');
  expect(boundary.setSelectedTable).toHaveBeenCalledWith('Table 1', 'Dining');
  expect(boundary.post.mock.invocationCallOrder[0])
    .toBeLessThan(boundary.setSelectedTable.mock.invocationCallOrder[0]);
});

it('choosing another table leaves the booking held and the floor in place', async () => {
  const dialog = await tapHeldTable();
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /choose another table/i })); });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(boundary.post).not.toHaveBeenCalled();
  expect(boundary.setSelectedTable).not.toHaveBeenCalled();
  expect(screen.queryByText('REGISTER')).toBeNull();
});

it('a refused seat keeps the host on the floor with the reason', async () => {
  boundary.post.mockRejectedValue(new Error('Not permitted'));
  const dialog = await tapHeldTable();
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /seat guest/i })); });
  await waitFor(() => expect(showToast.error).toHaveBeenCalledWith('Not permitted'));
  expect(boundary.setSelectedTable).not.toHaveBeenCalled();
  expect(screen.queryByText('REGISTER')).toBeNull();
});
