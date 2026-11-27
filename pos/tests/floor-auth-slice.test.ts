import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createAuthSlice } from '../src/store/slices/auth-slice';

// Theme T4 (fork #62): a Guest on a paired, open-till tablet stays in the in-app PIN pad.

const boundary = vi.hoisted(() => ({
  readLoggedUser: vi.fn(),
  getUserRoles: vi.fn(),
}));

vi.mock('@ury/core', () => ({
  readLoggedUser: boundary.readLoggedUser,
  getUserRoles: boundary.getUserRoles,
}));

let location: { href: string };

beforeEach(() => {
  vi.clearAllMocks();
  boundary.readLoggedUser.mockResolvedValue({user: null, networkFailure: false});
  location = { href: '/pos/order' };
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location;
      return Reflect.get(target, key, target);
    },
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as Window & { floor_pad?: unknown }).floor_pad;
});

function setFloorPad(value: { paired: boolean; till_open: boolean }) {
  Object.defineProperty(window, 'floor_pad', { configurable: true, value });
}

function makeStore() {
  let state: Record<string, unknown>;
  const set = (value: Record<string, unknown>) => { state = { ...state, ...value }; };
  const get = () => state;
  state = createAuthSlice(set as never, get as never, undefined as never) as unknown as Record<string, unknown>;
  return () => state;
}

it('keeps a paired, open-till Guest in the in-app floor pad flow', async () => {
  setFloorPad({ paired: true, till_open: true });
  const state = makeStore();
  await (state().checkAuth as () => Promise<void>)();
  expect(state().user).toBeNull();
  expect(state().isLoading).toBe(false);
  expect(location.href).toBe('/pos/order');
});

it.each([
  ['unpaired tablet', { paired: false, till_open: true }],
  ['closed till', { paired: true, till_open: false }],
])('sends a Guest on a %s to local sign-in', async (_name, floorPad) => {
  setFloorPad(floorPad);
  const state = makeStore();
  await (state().checkAuth as () => Promise<void>)();
  expect(state().user).toBeNull();
  expect(location.href).toBe('/pos/order');
});

it('sends a Guest without any floor context to local sign-in', async () => {
  const state = makeStore();
  await (state().checkAuth as () => Promise<void>)();
  expect(location.href).toBe('/pos/order');
});
