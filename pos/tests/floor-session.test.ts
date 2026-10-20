import { beforeEach, describe, expect, it, vi } from 'vitest';
import { floorSignOut } from '../src/lib/floor-session';

const boundary = vi.hoisted(() => ({
  clearCashierStorage: vi.fn(),
  clearAppStorage: vi.fn(),
  disconnectQzPrinter: vi.fn(),
}));

vi.mock('@ury/core', () => ({
  clearAppStorage: boundary.clearAppStorage,
  disconnectQzPrinter: boundary.disconnectQzPrinter,
}));
vi.mock('../src/offline/cashier', () => ({
  clearCashierStorage: boundary.clearCashierStorage,
}));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem('userAuth', 'true');
  boundary.clearCashierStorage.mockRejectedValue(new Error('logout failed online'));
  boundary.clearAppStorage.mockImplementation(async () => {
    localStorage.removeItem('userAuth');
  });
});

it('clears local state, disconnects QZ, and returns to the pad after logout failure', async () => {
  const replacement = vi.fn();
  const browserWindow = window;
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return { replace: replacement };
      return Reflect.get(target, key, target);
    },
  }));
  try {
    await floorSignOut();
    expect(boundary.clearAppStorage).toHaveBeenCalledExactlyOnceWith();
    expect(localStorage.getItem('userAuth')).toBeNull();
    expect(boundary.disconnectQzPrinter).toHaveBeenCalledExactlyOnceWith();
    expect(replacement).toHaveBeenCalledExactlyOnceWith('/pos/order');
    expect(sessionStorage.getItem('ury_floor_logout_failed')).toBe('true');
  } finally {
    vi.unstubAllGlobals();
  }
});
