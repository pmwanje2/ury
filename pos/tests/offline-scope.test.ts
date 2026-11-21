import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { lazyModule } from './helpers/lazy';

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
  const { syncOfflineCapture } = await lazyModule('src/pwa/register-offline.ts', 'opt-in service worker registration');
  await syncOfflineCapture({ ury_offline_capture: 1 });
  expect(registered).toEqual([{ url: '/ury-pos-sw.js', scope: '/pos' }]);
});

// Another app on the same site may own the root /sw.js (POS Awesome registers
// /sw.js?v=<build> at scope /). The URY worker uses its own name so neither
// app's registration or Service-Worker-Allowed header replaces the other's.
test('the POS worker never claims the site-wide /sw.js another app may own', async () => {
  const { syncOfflineCapture } = await lazyModule('src/pwa/register-offline.ts', 'opt-in service worker registration');
  await syncOfflineCapture({ ury_offline_capture: 1 });
  expect(registered.map((entry) => new URL(entry.url, ORIGIN).pathname)).not.toContain('/sw.js');
  const page = readPage();
  expect(page).not.toMatch(/self\.path == ["']sw\.js["']/);
  const viteConfig = readFileSync(resolve('vite.config.ts'), 'utf8');
  expect(viteConfig).not.toMatch(/served from \/sw\.js/);
});

const readPage = () => {
  expect(existsSync(resolve('../ury/offline_sw.py')), 'the /ury-pos-sw.js page renderer is not implemented').toBe(true);
  return readFileSync(resolve('../ury/offline_sw.py'), 'utf8');
};

test('the /ury-pos-sw.js response cannot widen the worker beyond /pos', () => {
  const page = readPage();
  expect(page).toContain('response.headers["Service-Worker-Allowed"] = "/pos"');
});

test('hooks.py describes the service worker scope as /pos', () => {
  const hooks = readFileSync(resolve('../ury/hooks.py'), 'utf8');
  const start = hooks.indexOf('# /ury-pos-sw.js is the POS service worker.');
  expect(start).toBeGreaterThanOrEqual(0);
  const comment = hooks.slice(start, hooks.indexOf('page_renderer', start));
  expect(comment).toContain('"/pos"');
  expect(comment).not.toContain('Scope "/"');
  expect(hooks).toContain('page_renderer = ["ury.offline_sw.OfflineServiceWorkerPage"]');
});

// v16 ships a Serve staff worker at /ury/serve/sw.js (scope /ury/serve/) whose
// after_request hook tags only its own paths. Neither worker may claim the other.
test('the POS worker and the v16 Serve worker do not claim each other\'s scope', async () => {
  const { SERVE_SCOPE, SERVE_SW_URL } = await import('../../serve/src/pwa');
  const servePython = readFileSync(resolve('../ury/ury/controllers/serve_pwa.py'), 'utf8');
  const serveWorker = readFileSync(resolve('../serve/public/sw.js'), 'utf8');
  const page = readPage();
  const { syncOfflineCapture } = await lazyModule('src/pwa/register-offline.ts', 'opt-in service worker registration');
  await syncOfflineCapture({ ury_offline_capture: 1 });
  const pos = registered[0];
  expect(pos).toEqual({ url: '/ury-pos-sw.js', scope: '/pos' });
  const inScope = (scope: string, path: string) => path === scope.replace(/\/$/, '') || path.startsWith(scope.endsWith('/') ? scope : scope + '/');
  // The POS worker cannot control Serve pages, and Serve cannot control /pos.
  expect(inScope(pos.scope!, SERVE_SCOPE)).toBe(false);
  expect(inScope(pos.scope!, '/ury/serve/tables')).toBe(false);
  expect(inScope(SERVE_SCOPE, '/pos')).toBe(false);
  expect(inScope(SERVE_SCOPE, '/pos/tables')).toBe(false);
  // Distinct scripts: the Serve after_request tagging never touches the POS
  // worker, and the POS page renderer serves only ury-pos-sw.js with its own /pos ceiling.
  expect(pos.url).not.toBe(SERVE_SW_URL);
  const servePaths = servePython.slice(servePython.indexOf('_SERVE_SW_PATHS'), servePython.indexOf('def after_request'));
  expect(servePaths).toContain('"/ury/serve/sw.js"');
  expect(servePaths).not.toMatch(/["']\/?sw\.js["']/);
  expect(servePaths).not.toMatch(/ury-pos-sw/);
  expect(page).toContain('return self.path == "ury-pos-sw.js"');
  expect(page).toContain('response.headers["Service-Worker-Allowed"] = "/pos"');
  // The Serve worker only deletes ury-serve- caches; the POS purge only ury-pos-.
  expect(serveWorker).toContain('CACHE_PREFIX = "ury-serve-"');
});

test('a POS purge leaves the Serve worker and its caches alone', async () => {
  const servePurge: string[] = [];
  vi.stubGlobal('navigator', {
    serviceWorker: {
      controller: null,
      register: async () => ({}),
      addEventListener: () => undefined,
      getRegistrations: async () => [{
        scope: `${ORIGIN}/ury/serve/`, active: null,
        unregister: async () => { servePurge.push('unregister:/ury/serve/'); return true; },
      }, {
        scope: `${ORIGIN}/pos`, active: null,
        unregister: async () => { unregistered.push('/pos'); return true; },
      }],
    },
  });
  vi.stubGlobal('caches', {
    keys: async () => ['ury-serve-shell-v1', 'ury-pos-reads'],
    delete: async (name: string) => { deleted.push(name); return true; },
  });
  const { purgeOfflineSession } = await import('../../packages/core/src/storage');
  expect(purgeOfflineSession, 'core has no offline session purge').toBeTypeOf('function');
  await purgeOfflineSession();
  expect(servePurge).toEqual([]);
  expect(unregistered).toEqual(['/pos']);
  expect(deleted).toEqual(['ury-pos-reads']);
});

test('flag off removes only the URY POS worker and ury-pos caches', async () => {
  const { syncOfflineCapture } = await lazyModule('src/pwa/register-offline.ts', 'opt-in service worker registration');
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
  const { purgeOfflineSession } = await import('../../packages/core/src/storage');
  expect(purgeOfflineSession, 'core has no offline session purge').toBeTypeOf('function');
  void purgeOfflineSession();
  await vi.waitFor(() => {
    expect(unregistered).toEqual(['/pos']);
    expect(deleted.sort()).toEqual([
      `ury-pos-precache-v2-${ORIGIN}/pos`,
      'ury-pos-reads',
      'ury-pos-shell',
    ]);
  });
});
