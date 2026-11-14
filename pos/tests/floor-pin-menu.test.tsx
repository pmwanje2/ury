import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import Header from '../src/components/Header';
import { initI18n } from '../src/i18n';

// M23: Staff PINs and Pair this tablet only make sense when the till has
// floor PIN sign-in on. With write permission but PIN sign-in off, the menu
// says so in plain language instead of offering actions the server refuses.

const fixtures = vi.hoisted(() => ({
  callGet: vi.fn(),
  root: {
    user: { name: 'manager@example.test', full_name: 'Manager', roles: ['URY Manager'] },
    orderSearchQuery: '',
    setOrderSearchQuery: vi.fn(),
  },
  pos: {
    searchQuery: '',
    setSearchQuery: vi.fn(),
    setShowVoluntaryClosing: vi.fn(),
    posProfile: { name: 'POS-1', custom_floor_pin_login: 0 } as Record<string, unknown>,
  },
}));

vi.mock('@ury/core', () => ({
  call: { get: fixtures.callGet, post: vi.fn() },
  parseFrappeError: (_error: unknown, fallback: string) => fallback,
  resolveBrand: () => ({ name: 'URY POS', logo: '/assets/ury/pos/ury_pos.png' }),
}));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (selector?: (state: typeof fixtures.root) => unknown) =>
    selector ? selector(fixtures.root) : fixtures.root,
}));
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (selector?: (state: typeof fixtures.pos) => unknown) =>
    selector ? selector(fixtures.pos) : fixtures.pos,
}));
vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: () => ({ rows: [], replaying: false, offline: false }),
}));
vi.mock('../src/offline/cashier', () => ({ clearCashierStorage: vi.fn() }));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.clearAllMocks();
  await initI18n('en');
  fixtures.callGet.mockImplementation(async (method: string) =>
    method === 'frappe.client.has_permission' ? { message: { has_permission: true } } : { message: [] });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function openManagerMenu() {
  await act(async () => root.render(<MemoryRouter initialEntries={['/pos']}><Header /></MemoryRouter>));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  const manager = [...container.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Manager')!;
  await act(async () => manager.click());
}

it('with PIN sign-in off, a manager sees a plain-language disabled state, not the actions', async () => {
  fixtures.pos.posProfile = { name: 'POS-1', custom_floor_pin_login: 0 };
  await openManagerMenu();
  expect(container.textContent).toContain('Floor PIN sign-in is off for this till');
  expect(container.textContent).not.toContain('Staff PINs');
  expect(container.textContent).not.toContain('Pair this tablet');
});

it('with PIN sign-in on and write permission, both actions show', async () => {
  fixtures.pos.posProfile = { name: 'POS-1', custom_floor_pin_login: 1 };
  await openManagerMenu();
  expect(container.textContent).toContain('Staff PINs');
  expect(container.textContent).toContain('Pair this tablet');
  expect(container.textContent).not.toContain('Floor PIN sign-in is off');
});

it('without write permission nothing about PINs shows, whatever the setting', async () => {
  fixtures.callGet.mockImplementation(async () => ({ message: { has_permission: false } }));
  for (const flag of [0, 1]) {
    fixtures.pos.posProfile = { name: 'POS-1', custom_floor_pin_login: flag };
    await openManagerMenu();
    expect(container.textContent).not.toContain('Staff PINs');
    expect(container.textContent).not.toContain('Floor PIN sign-in is off');
    await act(async () => root.render(<></>));
  }
});
