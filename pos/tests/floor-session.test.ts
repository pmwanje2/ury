import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { hasFloorSessionMarker, setFloorSessionMarker } from '../src/lib/floor-session';

// Theme T4 (fork #62 e56a8211/f6a8d7b9): ending a shared-tablet session always
// clears the tab and returns to the floor pad, even when the server logout fails.

const guard = vi.hoisted(() => vi.fn(async (run: () => Promise<void>) => run()));
vi.mock('../src/offline/outbox', async (original) => ({
  ...await original<typeof import('../src/offline/outbox')>(), withEmptyOutbox: guard,
}));

const boundary = vi.hoisted(() => ({
  logout: vi.fn(),
  disconnectQzPrinter: vi.fn(),
}));

vi.mock('@ury/core', () => ({
  getSessionUser: () => 'waiter@example.test',
  logout: boundary.logout,
  disconnectQzPrinter: boundary.disconnectQzPrinter,
  watchIdle: () => () => {},
}));

type FloorSession = {
  floorSignOut: () => Promise<string>;
  isStaySignedIn: (user: { roles: string[] }, profile: unknown) => boolean;
  isFloorPadActive: () => boolean;
};

async function floorSession(): Promise<FloorSession> {
  const path = '../src/lib/floor-session';
  const mod = await import(/* @vite-ignore */ path).catch(() => null);
  expect(mod, 'pos/src/lib/floor-session.ts must provide the floor session seam').not.toBeNull();
  return mod as FloorSession;
}

let replacement: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  setFloorSessionMarker('waiter@example.test', false);
  localStorage.setItem('ury_language', 'fr');
  sessionStorage.setItem('posProfile', '{"name":"POS-1"}');
  replacement = vi.fn();
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return { replace: replacement };
      return Reflect.get(target, key, target);
    },
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as Window & { floor_pad?: unknown }).floor_pad;
});

it('clears the tab, disconnects QZ and returns to the pad after a logout failure', async () => {
  boundary.logout.mockRejectedValue(new Error('logout failed online'));
  const { floorSignOut } = await floorSession();
  await floorSignOut();
  expect(boundary.logout).toHaveBeenCalledOnce();
  expect(sessionStorage.getItem('userAuth')).toBeNull();
  expect(sessionStorage.getItem('posProfile')).toBeNull();
  expect(localStorage.getItem('ury_language')).toBe('fr');
  expect(boundary.disconnectQzPrinter).toHaveBeenCalledExactlyOnceWith();
  expect(replacement).toHaveBeenCalledExactlyOnceWith('/pos/order');
  expect(localStorage.getItem('ury_floor_logout_failed')).toBe('waiter@example.test');
});

it('treats a logout that resolves false as failed and sets the failed-logout marker', async () => {
  // @ury/core logout() catches the server failure, purges the offline session and resolves false.
  boundary.logout.mockResolvedValue(false);
  const { floorSignOut } = await floorSession();
  await floorSignOut();
  expect(boundary.logout).toHaveBeenCalledOnce();
  expect(localStorage.getItem('ury_floor_logout_failed')).toBe('waiter@example.test');
  expect(sessionStorage.getItem('userAuth')).toBeNull();
  expect(sessionStorage.getItem('posProfile')).toBeNull();
  expect(boundary.disconnectQzPrinter).toHaveBeenCalledExactlyOnceWith();
  expect(replacement).toHaveBeenCalledExactlyOnceWith('/pos/order');
});

it('clears a stale failed-logout marker when logout succeeds', async () => {
  boundary.logout.mockResolvedValue(undefined);
  localStorage.setItem('ury_floor_logout_failed', 'waiter@example.test');
  const { floorSignOut } = await floorSession();
  await floorSignOut();
  expect(boundary.disconnectQzPrinter).toHaveBeenCalledExactlyOnceWith();
  expect(replacement).toHaveBeenCalledExactlyOnceWith('/pos/order');
  expect(localStorage.getItem('ury_floor_logout_failed')).toBeNull();
  expect(sessionStorage.getItem('userAuth')).toBeNull();
});

it('treats a configured stay-signed-in role as exempt and anything else as not', async () => {
  const { isStaySignedIn } = await floorSession();
  const profile = { custom_floor_stay_signed_in_roles: [{ role: 'URY Manager' }] };
  expect(isStaySignedIn({ roles: ['URY Captain', 'URY Manager'] }, profile)).toBe(true);
  expect(isStaySignedIn({ roles: ['URY Captain'] }, profile)).toBe(false);
  expect(isStaySignedIn({ roles: ['URY Captain'] }, {})).toBe(false);
  expect(isStaySignedIn({ roles: ['URY Captain'] }, null)).toBe(false);
});

it('is active only when the tablet is paired and its till is open', async () => {
  const { isFloorPadActive } = await floorSession();
  expect(isFloorPadActive()).toBe(false);
  for (const [pad, active] of [
    [{ paired: true, till_open: true }, true],
    [{ paired: true, till_open: false }, false],
    [{ paired: false, till_open: true }, false],
  ] as const) {
    Object.defineProperty(window, 'floor_pad', { configurable: true, value: pad });
    expect(isFloorPadActive()).toBe(active);
  }
});

it('disconnects the printer and leaves the floor even when stored markers cannot be removed', async () => {
  boundary.logout.mockResolvedValue(true);
  const { floorSignOut } = await floorSession();
  const blocked = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
    throw new DOMException('Blocked', 'SecurityError');
  });
  try {
    await expect(floorSignOut()).resolves.toBe('signed_out');
    expect(boundary.disconnectQzPrinter).toHaveBeenCalledOnce();
    expect(replacement).toHaveBeenCalledWith('/pos/order');
  } finally {
    blocked.mockRestore();
  }
});

it('preserves the session while offline orders await sync', async () => {
  guard.mockRejectedValueOnce(new Error('Offline outbox has pending items'));
  const { floorSignOut } = await floorSession();
  await expect(floorSignOut()).resolves.toBe('blocked');
  expect(boundary.logout).not.toHaveBeenCalled();
  expect(hasFloorSessionMarker('waiter@example.test')).toBe(true);
  expect(replacement).not.toHaveBeenCalled();
});

it('retains confirmation on reload and rejects a copy inherited from the opener', () => {
  const user = 'waiter@example.test';
  window.name = 'unrelated-window';
  setFloorSessionMarker(user, false);
  const original = window.name;
  expect(original).toMatch(/^ury-floor-tab:/);
  expect(JSON.parse(sessionStorage.getItem('userAuth')!)).toMatchObject({ user, tabId: original });
  expect(hasFloorSessionMarker(user)).toBe(true);
  expect(hasFloorSessionMarker('other@example.test')).toBe(false);
  window.name = '';
  expect(hasFloorSessionMarker(user)).toBe(false);
  window.name = 'another-window';
  expect(hasFloorSessionMarker(user)).toBe(false);
});
