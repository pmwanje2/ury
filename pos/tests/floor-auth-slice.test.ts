import { beforeEach, expect, it, vi } from 'vitest';
import { createAuthSlice } from '../src/store/slices/auth-slice';

const boundary = vi.hoisted(() => ({
  readLoggedUser: vi.fn(),
  getSessionUser: vi.fn(),
  getUserRoles: vi.fn(),
  floorPad: { paired: true, till_open: true },
}));

vi.mock('@ury/core', () => ({
  readLoggedUser: boundary.readLoggedUser,
  getSessionUser: boundary.getSessionUser,
  getUserRoles: boundary.getUserRoles,
}));
vi.mock('../src/pwa/register-offline', () => ({ offlineShellActive: () => false }));
vi.mock('../src/lib/floor-session', () => ({
  getFloorPad: () => boundary.floorPad,
}), { virtual: true });

beforeEach(() => {
  vi.clearAllMocks();
  boundary.readLoggedUser.mockResolvedValue({ user: null, networkFailure: false });
  boundary.getSessionUser.mockReturnValue(null);
  boundary.floorPad = { paired: true, till_open: true };
});

function makeStore() {
  let state: Record<string, unknown>;
  const set = (value: Record<string, unknown>) => { state = { ...state, ...value }; };
  const get = () => state;
  state = createAuthSlice(set as never, get as never) as unknown as Record<string, unknown>;
  return state;
}

it('keeps a paired, open-till Guest in the in-app floor pad flow', async () => {
  const location = { href: '/pos/order' };
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location;
      return Reflect.get(target, key, target);
    },
  }));
  try {
    const state = makeStore();
    await (state.checkAuth as () => Promise<void>)();
    expect(state.user).toBeNull();
    expect(location.href).toBe('/pos/order');
  } finally {
    vi.unstubAllGlobals();
  }
});

it.each([
  ['unpaired tablet', { paired: false, till_open: true }],
  ['closed till', { paired: true, till_open: false }],
])('sends a Guest on a %s to native /login', async (_name, floorPad) => {
  boundary.floorPad = floorPad;
  const location = { href: '/pos/order' };
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location;
      return Reflect.get(target, key, target);
    },
  }));
  try {
    const state = makeStore();
    await (state.checkAuth as () => Promise<void>)();
    expect(state.user).toBeNull();
    expect(location.href).toBe('/login?redirect-to=%2Fpos');
  } finally {
    vi.unstubAllGlobals();
  }
});
