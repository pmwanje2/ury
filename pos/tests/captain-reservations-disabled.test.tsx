import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Button } from '@ury/ui';
import { initI18n } from '../src/i18n';
import CaptainTables from '../src/captain/pages/CaptainTables';

// On a branch that has not enabled reservations (ury-erp/ury#358), the
// waiter floor keeps its pre-merge ownership rules: an occupied table with
// no resolvable owner stays closed to an ordinary waiter, and a free table
// opens without a reservation lock round trip.
const boundary = vi.hoisted(() => ({
  get: vi.fn(), toastError: vi.fn(),
  table: { name: 'T-05', occupied: 0 },
  reservationEnabled: undefined as boolean | undefined,
}));

vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  return { ...actual, getSessionUser: () => 'waiter@example.com', call: { ...actual.call, get: boundary.get, post: vi.fn() } };
});
vi.mock('@ury/ui', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/ui')>();
  return { ...actual, showToast: { ...actual.showToast, error: boundary.toastError } };
});
vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: unknown) => unknown) => {
    const state = { user: { name: 'waiter@example.com', roles: ['URY Captain'] } };
    return selector ? selector(state) : state;
  },
}));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => { throw new Error('Offline'); } }));
vi.mock('../src/captain/hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({
    context: { user: 'waiter@example.com', branch: 'Branch A', rooms: [], roles: [], pos_profile: null,
      reservation_enabled: boundary.reservationEnabled },
    capabilities: { canAccessOtherCaptainsTables: false },
    branch: 'Branch A', rooms: [], isLoading: false, error: null,
  }),
}));
vi.mock('../src/lib/table-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/table-api')>(),
  getRooms: vi.fn(async () => [{ name: 'Dining', branch: 'Branch A' }]),
  getTables: vi.fn(async () => [{ ...boundary.table, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining', table_shape: 'Square' }]),
}));
vi.mock('../src/captain/lib/captain-table-api', () => ({
  getActiveTableOrders: vi.fn(async () => new Map()),
  getUpcomingBookings: vi.fn(async () => new Map()),
  getUserFullNames: vi.fn(async () => new Map()),
}));
vi.mock('../src/captain/components/CaptainTableCard', () => ({
  default: ({ table, onTap }: { table: { name: string }; onTap: () => void }) => (
    <Button onClick={onTap}>{table.name}</Button>
  ),
}));
vi.mock('../src/lib/floor-session', () => ({ floorSignOut: vi.fn(async () => {}) }));

let container: HTMLDivElement;
let root: Root;

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ message: [] }) })));
  await initI18n('en');
  boundary.toastError.mockReset();
  boundary.get.mockReset().mockImplementation(async (method: string) => {
    if (method === 'ury.ury.api.ury_server_time.get_server_time') return { message: '2026-11-16 12:00:00' };
    if (method === 'ury.ury.api.table_reservation.get_branch_reservation_settings') {
      return { message: { enable_reservation: 0, buffer_time: 30, grace_period: 15 } };
    }
    // No open bill on the table: the server returns a new, unsaved invoice.
    if (method.endsWith('.get_order_invoice')) return { message: { name: null, docstatus: 0 } };
    throw new Error(`Unexpected method ${method}`);
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function tapTable() {
  await act(async () => root.render(
    <MemoryRouter initialEntries={['/order']}>
      <Routes>
        <Route path="/order" element={<CaptainTables />} />
        <Route path="/order/table/:table" element={<div>Order screen</div>} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  ));
  const button = await vi.waitFor(() => {
    const found = [...container.querySelectorAll('button')].find(b => b.textContent === 'T-05');
    expect(found).toBeDefined();
    return found!;
  });
  await act(async () => button.click());
}

function reservationCalls() {
  return boundary.get.mock.calls.map(([m]) => m as string)
    .filter(m => m.startsWith('ury.ury.api.table_reservation.') && !m.endsWith('get_branch_reservation_settings'));
}

it('a free table opens directly without a reservation lock read', async () => {
  await tapTable();
  await vi.waitFor(() => expect(container.querySelector('[data-testid="location"]')!.textContent).toBe('/order/table/T-05'));
  expect(reservationCalls()).toEqual([]);
});

it('an occupied table with no resolvable owner stays closed to an ordinary waiter', async () => {
  boundary.table = { name: 'T-05', occupied: 1 };
  await tapTable();
  await vi.waitFor(() => expect(boundary.toastError).toHaveBeenCalled());
  expect(container.querySelector('[data-testid="location"]')!.textContent).toBe('/order');
  expect(reservationCalls()).toEqual([]);
  boundary.table = { name: 'T-05', occupied: 0 };
});

it('a branch that enabled reservations keeps the upstream lock check on a free table', async () => {
  boundary.reservationEnabled = true;
  const base = boundary.get.getMockImplementation()!;
  boundary.get.mockImplementation(async (method: string, args: unknown) =>
    method === 'ury.ury.api.table_reservation.check_table_reservation' ? { message: null } : base(method, args));
  await tapTable();
  await vi.waitFor(() => expect(container.querySelector('[data-testid="location"]')!.textContent).toBe('/order/table/T-05'));
  expect(reservationCalls()).toEqual(['ury.ury.api.table_reservation.check_table_reservation']);
  boundary.reservationEnabled = undefined;
});
