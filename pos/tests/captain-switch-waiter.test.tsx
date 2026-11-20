import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CaptainTables from '../src/captain/pages/CaptainTables';

// Theme T4 (fork #62): the Captain Tables header offers a translated waiter switch
// that ends the shared-tablet session.

vi.mock('../src/i18n', () => ({
  t: (key: string) => (key === 'header.switch_waiter' ? 'Translated waiter switch' : key),
}));

const boundary = vi.hoisted(() => ({ floorSignOut: vi.fn() }));

vi.mock('../src/lib/floor-session', () => ({ floorSignOut: boundary.floorSignOut }));
vi.mock('../src/components/ServiceRequestAlert', () => ({ useOpenServiceRequestTables: () => new Map() }));
vi.mock('../src/captain/hooks/useCaptainContext', () => ({
  useCaptainContext: () => ({
    context: { user: 'captain@example.test' },
    capabilities: { canAccessOtherCaptainsTables: false },
    branch: 'Branch A',
    rooms: [{ name: 'Dining' }],
    isLoading: false,
    error: null,
  }),
}));
vi.mock('../src/lib/table-api', () => ({
  getRooms: vi.fn().mockResolvedValue([{ name: 'Dining' }]),
  getTables: vi.fn().mockResolvedValue([]),
  checkTableReservation: vi.fn().mockResolvedValue(null),
}));
vi.mock('../src/captain/lib/captain-table-api', () => ({
  getActiveTableOrders: vi.fn().mockResolvedValue(new Map()),
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

it('offers the translated waiter switch in the Captain header and returns to the floor pad', async () => {
  await act(async () => root.render(<MemoryRouter><CaptainTables /></MemoryRouter>));
  const button = [...container.querySelectorAll('button')]
    .find((candidate) => candidate.textContent?.trim() === 'Translated waiter switch');
  expect(button, 'Switch waiter button').toBeDefined();
  await act(async () => button!.click());
  expect(boundary.floorSignOut).toHaveBeenCalledExactlyOnceWith();
});
