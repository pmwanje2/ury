import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, expect, test, vi } from 'vitest';

const RUNTIME_CACHE = resolve('pos/src/pwa/runtime-caching.ts');
const REGISTER_OFFLINE = resolve('pos/src/pwa/register-offline.ts');

const ALLOWED_GETS = [
  'https://ury.localhost/api/method/ury.ury_pos.api.getRestaurantMenu?pos_profile=Bar&order_type=Take+Away',
  'https://ury.localhost/api/method/ury.ury_pos.api.getPosProfile',
  'https://ury.localhost/api/method/ury.ury_pos.api.getModeOfPayment',
  'https://ury.localhost/api/resource/URY%20Table?fields=%5B%22name%22%5D',
  'https://ury.localhost/api/resource/URY%20Table/T-1',
  'https://ury.localhost/api/resource/POS%20Profile/Salama%20POS',
  'https://ury.localhost/api/method/frappe.client.get_list?doctype=URY+Table&fields=%5B%22name%22%5D',
  'https://ury.localhost/pos',
  'https://ury.localhost/pos/pos',
  'https://ury.localhost/pos/tables',
];

const DENIED: Array<[string, string]> = [
  ['https://ury.localhost/api/method/ury.ury_pos.api.getRestaurantMenu', 'POST'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.getPosProfile', 'POST'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.getModeOfPayment', 'PUT'],
  ['https://ury.localhost/api/resource/URY%20Table', 'POST'],
  ['https://ury.localhost/api/resource/URY%20Table/T-1', 'PUT'],
  ['https://ury.localhost/api/resource/POS%20Profile/Salama%20POS', 'DELETE'],
  ['https://ury.localhost/pos', 'POST'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.sync_order', 'GET'],
  ['https://ury.localhost/api/method/ury.ury.doctype.ury_order.ury_order.get_order_invoice?table=T-1', 'GET'],
  ['https://ury.localhost/api/method/logout', 'GET'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.getAggregatorItem?aggregator=X', 'GET'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.getMenuCourses', 'GET'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.getRestaurantMenuForReal', 'GET'],
  ['https://ury.localhost/api/resource/POS%20Invoice', 'GET'],
  ['https://ury.localhost/api/resource/Customer', 'GET'],
  ['https://ury.localhost/api/resource/URY%20Room', 'GET'],
  ['https://ury.localhost/api/resource/Currency/UGX', 'GET'],
  ['https://ury.localhost/api/method/frappe.client.get_list?doctype=POS+Invoice', 'GET'],
  ['https://ury.localhost/api/method/frappe.client.get_list?doctype=URY+Room', 'GET'],
  ['https://ury.localhost/api/method/frappe.client.get_list?doctype=URY+Table+Extra', 'GET'],
  ['https://ury.localhost/api/method/frappe.client.get_count?doctype=URY+Table', 'GET'],
  ['https://ury.localhost/api/method/frappe.client.get?doctype=URY+Table&name=T-1', 'GET'],
  ['https://ury.localhost/assets/ury/pos/assets/index-abc.js', 'GET'],
  ['https://ury.localhost/login', 'GET'],
  ['https://ury.localhost/api/method/ury.ury_pos.api.make_invoice', 'GET'],
];

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

test('Workbox runtimeCaching is network-first and excludes POST and non-allowlisted /api routes', async () => {
  expect(existsSync(RUNTIME_CACHE), 'pos/src/pwa/runtime-caching.ts must define the Workbox runtimeCaching config').toBe(true);
  const runtimeModule = '../src/pwa/runtime-caching.ts';
  const caching = await import(/* @vite-ignore */ runtimeModule);
  const routes = caching.workboxRuntimeCaching();
  expect(routes.length).toBeGreaterThan(0);
  for (const route of routes) {
    expect(route.method).toBe('GET');
    expect(route.handler).toBe('NetworkFirst');
    expect(route.method).not.toBe('POST');
  }
  expect(routes.some((route: { method?: string }) => route.method === 'POST')).toBe(false);

  for (const url of ALLOWED_GETS) {
    expect(caching.requestMatchesRuntimeCache(url, 'GET'), url).toBe(true);
  }
  for (const [url, method] of DENIED) {
    expect(caching.requestMatchesRuntimeCache(url, method), `${method} ${url}`).toBe(false);
  }

  const menuCache = caching.runtimeCacheNameFor(
    'https://ury.localhost/api/method/ury.ury_pos.api.getRestaurantMenu?pos_profile=Bar',
    'GET',
  );
  const shellCache = caching.runtimeCacheNameFor('https://ury.localhost/pos/pos', 'GET');
  const tableCache = caching.runtimeCacheNameFor(
    'https://ury.localhost/api/resource/URY%20Table?limit=0',
    'GET',
  );
  const profileCache = caching.runtimeCacheNameFor(
    'https://ury.localhost/api/resource/POS%20Profile/Salama%20POS',
    'GET',
  );
  const paymentCache = caching.runtimeCacheNameFor(
    'https://ury.localhost/api/method/ury.ury_pos.api.getModeOfPayment',
    'GET',
  );
  expect(menuCache).toBe('ury-pos-reads');
  expect(tableCache).toBe('ury-pos-reads');
  expect(profileCache).toBe('ury-pos-reads');
  expect(paymentCache).toBe('ury-pos-reads');
  expect(shellCache).toBe('ury-pos-shell');

  expect(caching.offlineCaptureEnabled(undefined)).toBe(false);
  expect(caching.offlineCaptureEnabled(null)).toBe(false);
  expect(caching.offlineCaptureEnabled({})).toBe(false);
  expect(caching.offlineCaptureEnabled({ ury_offline_capture: 0 })).toBe(false);
  expect(caching.offlineCaptureEnabled({ ury_offline_capture: '0' })).toBe(false);
  expect(caching.offlineCaptureEnabled({ ury_offline_capture: 1 })).toBe(true);
  expect(caching.offlineCaptureEnabled({ ury_offline_capture: '1' })).toBe(true);
  expect(caching.offlineCaptureEnabled({ ury_offline_capture: true })).toBe(true);
});

test('offline capture registers the service worker only when the till flag is on', async () => {
  expect(existsSync(REGISTER_OFFLINE), 'pos/src/pwa/register-offline.ts must gate service-worker registration').toBe(true);
  const registered: string[] = [];
  vi.stubGlobal('navigator', {
    serviceWorker: {
      controller: null,
      register: async (url: string) => {
        registered.push(url);
        return {};
      },
      ready: Promise.resolve({}),
      addEventListener: () => undefined,
      getRegistrations: async () => [],
    },
  });
  vi.stubGlobal('caches', {
    keys: async () => [],
    delete: async () => true,
  });

  const registerModule = '../src/pwa/register-offline.ts';
  const { syncOfflineCapture } = await import(/* @vite-ignore */ registerModule);
  await syncOfflineCapture({});
  expect(registered).toEqual([]);
  await syncOfflineCapture({ ury_offline_capture: 0 });
  expect(registered).toEqual([]);
  await syncOfflineCapture({ ury_offline_capture: 1 });
  expect(registered).toEqual(['/sw.js']);
});

test('precache URLs are rooted at the POS asset base because /sw.js is not served from that directory', async () => {
  const caching = await import(/* @vite-ignore */ '../src/pwa/runtime-caching.ts');
  expect(caching.precacheURLForServiceWorker('ury_pos.png')).toBe('/assets/ury/pos/ury_pos.png');
  expect(caching.precacheURLForServiceWorker('ury.ico')).toBe('/assets/ury/pos/ury.ico');
  expect(caching.precacheURLForServiceWorker('assets/index.js')).toBe('/assets/ury/pos/assets/index.js');
  expect(caching.precacheModifyURLPrefix()).toEqual({ '': '/assets/ury/pos/' });
  const viteConfig = readFileSync(resolve('pos/vite.config.ts'), 'utf8');
  expect(viteConfig).toContain('precacheModifyURLPrefix()');
});

test('a service worker that never activates does not block offline capture setup', async () => {
  vi.stubGlobal('navigator', {
    serviceWorker: {
      controller: {},
      register: async () => ({}),
      ready: new Promise(() => undefined),
      addEventListener: () => undefined,
      getRegistrations: async () => [],
    },
  });
  vi.stubGlobal('caches', { keys: async () => [], delete: async () => true });
  const { syncOfflineCapture } = await import(/* @vite-ignore */ '../src/pwa/register-offline.ts');
  const result = await Promise.race([
    syncOfflineCapture({ ury_offline_capture: 1 }).then(() => 'done'),
    new Promise((resolve) => setTimeout(() => resolve('blocked'), 2500)),
  ]);
  expect(result).toBe('done');
});
