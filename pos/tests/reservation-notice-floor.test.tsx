import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { showToast } from '@ury/ui';
import TableView from '../src/pages/Table';
import { initI18n } from '../src/i18n';

// Reserving from the floor honours the branch's Minimum Reservation Notice
// (Simphony TMS Parameters), read from the server, not a fixed margin.
const boundary = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), getDocList: vi.fn(),
  setSelectedTable: vi.fn(), setSelectedOrderType: vi.fn(),
  posProfile: { branch: 'Branch A', role_allowed_for_billing: [{ role: 'URY Cashier' }], transfer_role_permissions: [] },
}));
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
vi.mock('../src/store/root-store', () => ({
  useRootStore: (select: Function) => select({ user: { name: 'captain@fixture.invalid', roles: ['URY Captain'] } }),
}));
// The guest search is not under test: picking a guest hands the dialog a customer.
vi.mock('../src/components/CustomerPicker', () => ({
  CustomerPicker: ({ onChange }: { onChange: (customer: { id: string; name: string; phone: string }) => void }) => (
    <button type="button" onClick={() => onChange({ id: 'CUST-1', name: 'Grace', phone: '+256772123456' })}>
      Pick Grace
    </button>
  ),
}));

const RESERVATION = 'ury.ury.api.table_reservation.';

beforeEach(async () => {
  vi.resetAllMocks();
  sessionStorage.clear();
  // The site clock reads 12:00; only Date is faked.
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 10, 23, 12, 0, 0));
  await initI18n('en');
  vi.spyOn(showToast, 'error').mockImplementation(() => 'toast');
  vi.spyOn(showToast, 'success').mockImplementation(() => 'toast');
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
      return { message: { enable_reservation: 1, buffer_time: 30, grace_period: 15, min_reservation_notice: 30 } };
    }
    if (endpoint === `${RESERVATION}get_active_reservations`) return { message: [] };
    if (endpoint === `${RESERVATION}check_table_reservation`) return { message: null };
    if (endpoint.endsWith('.get_order_invoice')) return { message: null };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  boundary.post.mockResolvedValue({ message: 'TR-NEW' });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const booked = () => boundary.post.mock.calls.some(([endpoint]) => endpoint === `${RESERVATION}create_table_reservation`);

it("the floor's reserve dialog refuses a time inside the branch's notice and books one outside it", async () => {
  render(
    <MemoryRouter initialEntries={['/tables']}>
      <Routes>
        <Route path="/tables" element={<TableView />} />
      </Routes>
    </MemoryRouter>,
  );
  const actions = await waitFor(() => screen.getByRole('button', { name: /table actions/i }));
  await act(async () => { fireEvent.click(actions); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^reserve$/i })); });
  const dialog = await waitFor(() => screen.getByRole('dialog'));
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Pick Grace' })); });

  // The dialog opens on the current minute, 12:00: inside a 30 minute notice.
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /reserve table/i })); });
  await waitFor(() => expect(dialog.textContent).toMatch(/at least 30 minutes/));
  expect(booked()).toBe(false);

  await act(async () => {
    fireEvent.change(within(dialog).getByDisplayValue('12:00'), { target: { value: '12:30' } });
  });
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /reserve table/i })); });
  await waitFor(() => expect(booked()).toBe(true));
});
