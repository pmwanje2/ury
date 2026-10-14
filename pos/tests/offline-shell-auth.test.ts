// Real @ury/core client and frappe-js-sdk over real sockets; only the
// browser location and service-worker controller are stubbed.
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { AxiosInstance } from 'axios';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest';
import { auth } from '../../packages/core/src/frappe/client';
import { useRootStore } from '../src/store/root-store';

const cashier = 'cashier@ury.com';
const transport = (auth as unknown as { axios: AxiosInstance }).axios;
let server: http.Server;
let liveBase: string;
let deadBase: string;
let status = 200;
let location: { href: string };

beforeAll(async () => {
  server = http.createServer((_request, response) => {
    response.statusCode = status;
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify(status === 200 ? { message: cashier } : { exc_type: 'AuthenticationError' }));
  });
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
  liveBase = 'http://127.0.0.1:' + (server.address() as AddressInfo).port;
  const dead = http.createServer();
  await new Promise<void>((done) => dead.listen(0, '127.0.0.1', done));
  deadBase = 'http://127.0.0.1:' + (dead.address() as AddressInfo).port;
  await new Promise<void>((done) => dead.close(() => done()));
  transport.defaults.adapter = 'fetch';
});

afterAll(async () => {
  await new Promise<void>((done) => server.close(() => done()));
});

function controlledByPosWorker(controlled: boolean) {
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: controlled ? { controller: { scriptURL: 'http://localhost:3000/sw.js' } } : { controller: null },
  });
}

beforeEach(() => {
  status = 200;
  transport.defaults.baseURL = deadBase;
  document.cookie = 'user_id=' + encodeURIComponent(cashier) + '; Path=/';
  useRootStore.setState({ user: null, isLoading: false, error: null, posProfile: null });
  const browserWindow = window;
  (browserWindow as unknown as { frappe: unknown }).frappe = {
    boot: { user: { roles: ['URY Cashier'], full_name: 'URY Cashier' } },
  };
  location = { href: 'http://localhost:3000/pos/pos' };
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return location;
      const value = Reflect.get(target, key, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  }));
  controlledByPosWorker(true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  controlledByPosWorker(false);
  document.cookie = 'user_id=; Max-Age=0; Path=/';
});

test('a real transport failure on an opted-in till keeps the cookie session provisionally', async () => {
  await useRootStore.getState().checkAuth();
  expect(useRootStore.getState().user).toMatchObject({ name: cashier, roles: ['URY Cashier'], provisional: true });
  expect(location.href).toBe('http://localhost:3000/pos/pos');
});

test('a till that has not opted in still goes to login on a transport failure', async () => {
  controlledByPosWorker(false);
  await useRootStore.getState().checkAuth();
  expect(useRootStore.getState().user).toBeNull();
  expect(location.href).toBe('/login?redirect-to=%2Fpos');
});

test.each([401, 403])('HTTP %i always goes to login, even offline-capable and with navigator.onLine false', async (code) => {
  transport.defaults.baseURL = liveBase;
  status = code;
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
  try {
    await useRootStore.getState().checkAuth();
  } finally {
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
  }
  expect(useRootStore.getState().user).toBeNull();
  expect(location.href).toBe('/login?redirect-to=%2Fpos');
});

test('a Guest cookie is never kept', async () => {
  document.cookie = 'user_id=Guest; Path=/';
  await useRootStore.getState().checkAuth();
  expect(useRootStore.getState().user).toBeNull();
  expect(location.href).toBe('/login?redirect-to=%2Fpos');
});

test.each([401, 403])('reconnecting revalidates the provisional session and a revoked one (%i) goes to login', async (code) => {
  await useRootStore.getState().checkAuth();
  expect(useRootStore.getState().user?.provisional).toBe(true);
  transport.defaults.baseURL = liveBase;
  status = code;
  window.dispatchEvent(new Event('online'));
  await vi.waitFor(() => expect(location.href).toBe('/login?redirect-to=%2Fpos'));
  expect(useRootStore.getState().user).toBeNull();
});

test('reconnecting with a live session clears the provisional mark', async () => {
  await useRootStore.getState().checkAuth();
  transport.defaults.baseURL = liveBase;
  window.dispatchEvent(new Event('online'));
  await vi.waitFor(() => expect(useRootStore.getState().user?.provisional).toBeFalsy());
  expect(useRootStore.getState().user?.name).toBe(cashier);
  expect(location.href).toBe('http://localhost:3000/pos/pos');
});
