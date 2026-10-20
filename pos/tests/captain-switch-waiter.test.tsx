import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CaptainTables from '../src/captain/pages/CaptainTables';

const boundary = vi.hoisted(() => ({
  floorSignOut: vi.fn(),
  context: {
    user: 'captain@example.test',
    roles: ['URY Captain'],
    branch: 'Branch A',
    rooms: [],
    pos_profile: { name: 'POS-1', transfer_role_permissions: true, role_allowed_for_billing: false, role_restricted_for_table_order: false, remove_items: false, show_image: false, custom_enable_kot_reprint: false, custom_enable_multiple_cashier: false },
    role_restricted_for_table_order: false,
    opening_state: { pos_open: true },
  },
}));

vi.mock('../src/lib/floor-session', () => ({ floorSignOut: boundary.floorSignOut }), { virtual: true });
vi.mock('../src/captain/hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({
    context: boundary.context,
    capabilities: { canAccessOtherCaptainsTables: false, canTakeTableOrders: true },
    branch: 'Branch A', rooms: [], isLoading: false, error: null,
  }),
}));
vi.mock('../src/lib/table-api', () => ({
  getRooms: vi.fn().mockResolvedValue([{ name: 'Dining', branch: 'Branch A' }]),
  getTables: vi.fn().mockResolvedValue([]),
}));
vi.mock('../src/captain/lib/captain-table-api', () => ({
  getActiveTableOrders: vi.fn().mockResolvedValue(new Map()),
  getUpcomingBookings: vi.fn().mockResolvedValue(new Map()),
  getUserFullNames: vi.fn().mockResolvedValue(new Map()),
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it('offers Switch waiter in the Captain header and returns to the floor pad', async () => {
  await act(async () => root.render(<MemoryRouter><CaptainTables /></MemoryRouter>));
  const button = [...container.querySelectorAll('button')]
    .find((candidate) => candidate.textContent?.trim() === 'Switch waiter');
  expect(button).toBeDefined();
  await act(async () => button!.click());
  expect(boundary.floorSignOut).toHaveBeenCalledExactlyOnceWith();
});
