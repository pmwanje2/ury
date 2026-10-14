import { afterEach, beforeEach, expect, test, vi } from 'vitest';

vi.mock('../../packages/core/src/frappe/client', () => ({
  auth: { getLoggedInUser: vi.fn(), logout: vi.fn() },
  call: { get: vi.fn(), post: vi.fn() },
  db: { getDoc: vi.fn(), getDocList: vi.fn() },
}));

import { auth } from '../../packages/core/src/frappe/client';
import { useRootStore } from '../src/store/root-store';

const cashier = 'cashier@ury.com';

beforeEach(() => {
  document.cookie = `user_id=${encodeURIComponent(cashier)}; Path=/`;
  useRootStore.setState({ user: null, isLoading: false, error: null, posProfile: null });
  vi.mocked(auth.getLoggedInUser).mockRejectedValue(new Error('offline'));
  const browserWindow = window;
  (browserWindow as unknown as { frappe: unknown }).frappe = {
    boot: { user: { roles: ['URY Cashier'], full_name: 'URY Cashier' } },
  };
  const location = { href: 'http://ury.localhost/pos/pos' };
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location;
      return Reflect.get(target, key, target);
    },
  }));
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
});

afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  document.cookie = 'user_id=; Max-Age=0; Path=/';
});

test('an offline reload keeps the cached shell for the existing session', async () => {
  await useRootStore.getState().checkAuth();

  expect(useRootStore.getState().user).toMatchObject({
    name: cashier,
    roles: ['URY Cashier'],
  });
  expect(window.location.href).toBe('http://ury.localhost/pos/pos');
});

test('an online session miss still leaves the till for login', async () => {
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });

  await useRootStore.getState().checkAuth();

  expect(useRootStore.getState().user).toBeNull();
  expect(window.location.href).toBe('/login?redirect-to=%2Fpos');
});
