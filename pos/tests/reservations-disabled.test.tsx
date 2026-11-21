import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TableView from '../src/pages/Table';
import Footer from '../src/components/Footer';
import { initI18n } from '../src/i18n';

// Upstream ury-erp/ury#358 table reservations are opt-in per Branch
// (custom_enable_reservation). A branch that has not enabled them must not
// poll, lock-check or reconcile reservations from the floor, and a refused
// or missing reservation endpoint must never take the floor down.
const boundary = vi.hoisted(() => ({
  get: vi.fn(), getDocList: vi.fn(),
  enabled: 0,
  user: { name: 'cashier@fixture.invalid', roles: ['URY Cashier'] },
  posProfile: { branch: 'Branch A', transfer_role_permissions: [] as Array<{ role: string }> },
}));
vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: boundary.get, post: vi.fn() }, db: { getDocList: boundary.getDocList },
}));
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({ posProfile: boundary.posProfile, setSelectedTable: vi.fn(), setSelectedOrderType: vi.fn() }),
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: (select: Function) => select({ user: boundary.user }) }));

const RESERVATION = 'ury.ury.api.table_reservation.';
let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.resetAllMocks();
  sessionStorage.clear();
  boundary.enabled = 0;
  await initI18n('en');
  boundary.getDocList.mockImplementation(async (doctype, options) => {
    if (doctype === 'URY Room') return [{ name: 'Dining', branch: 'Branch A' }];
    if (options.fields[0] === 'count(name) as count') return [{ count: 1 }];
    if (doctype === 'URY Table') return [{
      name: 'Table 1', occupied: 1, latest_invoice_time: null, is_take_away: 0,
      restaurant_room: 'Dining', table_shape: 'Square',
    }];
    throw new Error(`Unexpected doctype: ${doctype}`);
  });
  boundary.get.mockImplementation(async (endpoint) => {
    if (endpoint === `${RESERVATION}get_branch_reservation_settings`) {
      return { message: { enable_reservation: boundary.enabled, buffer_time: 30, grace_period: 15 } };
    }
    if (endpoint.startsWith(RESERVATION)) throw new Error(`Reservations refused: ${endpoint}`);
    if (endpoint.endsWith('.get_order_invoice')) return { message: { name: 'INV-1', docstatus: 0 } };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function reservationCalls() {
  return boundary.get.mock.calls
    .map(([endpoint]) => endpoint as string)
    .filter((endpoint) => endpoint.startsWith(RESERVATION) && !endpoint.endsWith('get_branch_reservation_settings'));
}

it('a branch without reservations renders its floor and makes no reservation reads or polls', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  await act(async () => root.render(<MemoryRouter><TableView /></MemoryRouter>));
  await act(async () => { await vi.advanceTimersByTimeAsync(31000); });
  expect(container.textContent).toContain('Table 1');
  expect(reservationCalls()).toEqual([]);
});

it('a malformed reservation list on an enabled branch leaves the floor usable', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  boundary.enabled = 1;
  const base = boundary.get.getMockImplementation()!;
  boundary.get.mockImplementation(async (endpoint, args) => endpoint === `${RESERVATION}get_active_reservations`
    ? { message: { exc_type: 'PermissionError' } }
    : base(endpoint, args));
  await act(async () => root.render(<MemoryRouter><TableView /></MemoryRouter>));
  await act(async () => { await vi.advanceTimersByTimeAsync(16000); });
  expect(container.textContent).toContain('Table 1');
});

it('the footer offers Reservations only when the branch enabled them', async () => {
  await act(async () => root.render(<MemoryRouter><Footer /></MemoryRouter>));
  expect(container.textContent).not.toMatch(/Reservations/);
  boundary.enabled = 1;
  await act(async () => root.unmount());
  root = createRoot(container);
  await act(async () => root.render(<MemoryRouter><Footer /></MemoryRouter>));
  await vi.waitFor(() => expect(container.textContent).toMatch(/Reservations/));
});
