// @vitest-environment node
// Real workbox-strategies 7.4.1 NetworkFirst through the worker's routes.
import { beforeEach, expect, test, vi } from 'vitest';
import { installWorkerGlobals, ORIGIN, ownedResponse, TestExtendableEvent, type MemoryCacheStorage } from './helpers/sw-env';

let storage: MemoryCacheStorage;
const PROFILE = ORIGIN + '/api/method/ury.ury_pos.api.getPosProfile';

async function worker() {
  const { Router } = await import('workbox-routing');
  const { createOfflineWorker } = await import('../src/pwa/offline-sw-core');
  const router = new Router();
  const instance = createOfflineWorker({ origin: ORIGIN });
  for (const route of instance.routes) router.registerRoute(route);
  await instance.handleMessage({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  // Alice's earlier read, cached while her session was bound.
  await (await storage.open('ury-pos-reads')).put(PROFILE, ownedResponse('Alice prior cached profile', 'alice@ury.com'));
  return {
    instance,
    async get(href: string) {
      const request = new Request(href);
      const url = new URL(href);
      const event = new TestExtendableEvent('fetch');
      const { route } = router.findMatchingRoute({ url, request, sameOrigin: true, event: event as unknown as ExtendableEvent });
      const response = await route!.handler.handle({ request, event: event as unknown as ExtendableEvent, url });
      await event.settle();
      return response as Response;
    },
  };
}

function reply(status: number, body: string, delayMs = 0) {
  return vi.fn(() => new Promise<Response>((resolve) => setTimeout(
    () => resolve(new Response(body, { status, headers: { 'content-type': 'application/json' } })),
    delayMs,
  )));
}

beforeEach(async () => {
  vi.resetModules();
  storage = installWorkerGlobals();
  vi.unstubAllGlobals();
});

test.each([401, 403])('a denial (%i) that arrives after 3 s is returned, not the cached profile', async (code) => {
  vi.stubGlobal('fetch', reply(code, '{"exc_type":"AuthenticationError"}', 3300));
  const { get } = await worker();
  const response = await get(PROFILE);
  expect(response.status).toBe(code);
  expect(await response.text()).not.toContain('Alice');
}, 10000);

test.each([401, 403])('a denial (%i) purges the URY read and shell caches', async (code) => {
  await (await storage.open('ury-pos-shell')).put(ORIGIN + '/pos', new Response('shell'));
  await storage.open('desk-cache');
  vi.stubGlobal('fetch', reply(code, '{}'));
  const { get } = await worker();
  expect((await get(PROFILE)).status).toBe(code);
  expect((await storage.keys()).sort()).toEqual(['desk-cache']);
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
  await expect(get(PROFILE)).rejects.toThrow();
});

test('a genuine transport outage falls back to the cached read', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
  const { get } = await worker();
  const response = await get(PROFILE);
  expect(response.status).toBe(200);
  expect(await response.text()).toBe('Alice prior cached profile');
});

test('a fresh 200 replaces the cached read; errors are never cached', async () => {
  vi.stubGlobal('fetch', reply(200, 'Alice fresh profile'));
  const { get } = await worker();
  expect(await (await get(PROFILE)).text()).toBe('Alice fresh profile');
  expect(await storage.body('ury-pos-reads', PROFILE)).toBe('Alice fresh profile');
  vi.stubGlobal('fetch', reply(500, 'boom'));
  expect((await get(PROFILE)).status).toBe(500);
  expect(await storage.body('ury-pos-reads', PROFILE)).toBe('Alice fresh profile');
});

test.each([502, 503, 504, 520, 521, 522, 523, 524])(
  'a gateway status (%i) falls back to the cached read and does not store the error',
  async (code) => {
    vi.stubGlobal('fetch', reply(code, `gateway ${code}`));
    const { get } = await worker();
    const response = await get(PROFILE);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('Alice prior cached profile');
    expect(await storage.body('ury-pos-reads', PROFILE)).toBe('Alice prior cached profile');
  },
);

test('a gateway 502 for the POS shell falls back to the cached shell', async () => {
  vi.stubGlobal('fetch', reply(502, 'bad gateway'));
  const { get } = await worker();
  // Bind purges caches, so the shell entry has to be stored after it.
  await (await storage.open('ury-pos-shell')).put(ORIGIN + '/pos', ownedResponse('cached shell', 'alice@ury.com'));
  const response = await get(ORIGIN + '/pos');
  expect(response.status).toBe(200);
  expect(await response.text()).toBe('cached shell');
});

test.each([400, 401, 403, 404, 500])('HTTP %i is returned and is never the cached read', async (code) => {
  vi.stubGlobal('fetch', reply(code, `answer ${code}`));
  const { get } = await worker();
  const response = await get(PROFILE);
  expect(response.status).toBe(code);
  expect(await response.text()).toBe(`answer ${code}`);
});

test('HTTP 500 does not purge the cached read', async () => {
  vi.stubGlobal('fetch', reply(500, 'boom'));
  const { get } = await worker();
  expect((await get(PROFILE)).status).toBe(500);
  expect(await storage.body('ury-pos-reads', PROFILE)).toBe('Alice prior cached profile');
});
