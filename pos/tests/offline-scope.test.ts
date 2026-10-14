import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

const ORIGIN = 'https://ury.localhost';
let unregistered: string[];
let deleted: string[];
let registered: Array<{ url: string; scope?: string }>;

function stubBrowser() {
  unregistered = [];
  deleted = [];
  registered = [];
  const registration = (scope: string) => ({
    scope: `${ORIGIN}${scope}`,
    active: null,
    unregister: async () => {
      unregistered.push(scope);
      return true;
    },
  });
  vi.stubGlobal('navigator', {
    serviceWorker: {
      controller: {},
      register: async (url: string, options?: { scope?: string }) => {
        registered.push({ url, scope: options?.scope });
        return registration(options?.scope ?? '/');
      },
      addEventListener: () => undefined,
      getRegistrations: async () => [registration('/'), registration('/app/'), registration('/pos')],
    },
  });
  vi.stubGlobal('caches', {
    keys: async () => [
      'desk-cache',
      `workbox-precache-v2-${ORIGIN}/`,
      'ury-pos-reads',
      'ury-pos-shell',
      `ury-pos-precache-v2-${ORIGIN}/pos`,
    ],
    delete: async (name: string) => {
      deleted.push(name);
      return true;
    },
  });
}

beforeEach(stubBrowser);
afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

test('the POS worker is registered for the /pos scope only', async () => {
  const { syncOfflineCapture } = await import('../src/pwa/register-offline');
  await syncOfflineCapture({ ury_offline_capture: 1 });
  expect(registered).toEqual([{ url: '/sw.js', scope: '/pos' }]);
});

test('the /sw.js response cannot widen the worker beyond /pos', () => {
  const page = readFileSync(resolve('ury/offline_sw.py'), 'utf8');
  expect(page).toContain('response.headers["Service-Worker-Allowed"] = "/pos"');
});

test('flag off removes only the URY POS worker and ury-pos caches', async () => {
  const { syncOfflineCapture } = await import('../src/pwa/register-offline');
  await syncOfflineCapture({ ury_offline_capture: 0 });
  expect(registered).toEqual([]);
  expect(unregistered).toEqual(['/pos']);
  expect(deleted.sort()).toEqual([
    `ury-pos-precache-v2-${ORIGIN}/pos`,
    'ury-pos-reads',
    'ury-pos-shell',
  ]);
});

test('logout removes only the URY POS worker and ury-pos caches', async () => {
  const { clearAppStorage } = await import('../../packages/core/src/storage');
  clearAppStorage();
  await vi.waitFor(() => {
    expect(unregistered).toEqual(['/pos']);
    expect(deleted.sort()).toEqual([
      `ury-pos-precache-v2-${ORIGIN}/pos`,
      'ury-pos-reads',
      'ury-pos-shell',
    ]);
  });
});
