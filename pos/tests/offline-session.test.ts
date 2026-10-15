// @vitest-environment node
// Real workbox-strategies NetworkFirst through the worker routes. The
// sign-out race reproduces review #38 finding 1 at the worker boundary.
import { beforeEach, expect, test, vi } from 'vitest';
import { deferred, installWorkerGlobals, ORIGIN, ownedResponse, setCookieUser, TestExtendableEvent, type MemoryCacheStorage } from './helpers/sw-env';

let storage: MemoryCacheStorage;
const PROFILE = ORIGIN + '/api/method/ury.ury_pos.api.getPosProfile';

async function startWorker() {
  const { Router } = await import('workbox-routing');
  const { createOfflineWorker } = await import('../src/pwa/offline-sw-core');
  const router = new Router();
  const instance = createOfflineWorker({ origin: ORIGIN });
  for (const route of instance.routes) router.registerRoute(route);
  return {
    message: (data: unknown) => instance.handleMessage(data),
    get(href: string) {
      const request = new Request(href);
      const url = new URL(href);
      const event = new TestExtendableEvent('fetch');
      const { route } = router.findMatchingRoute({ url, request, sameOrigin: true, event: event as unknown as ExtendableEvent });
      return (async () => {
        const response = await route!.handler.handle({ request, event: event as unknown as ExtendableEvent, url });
        await event.settle();
        return response as Response;
      })();
    },
  };
}

const outage = () => vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
const ok = (body: string) => vi.fn(() => Promise.resolve(new Response(body, { status: 200 })));

beforeEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
  storage = installWorkerGlobals();
});

test('an Alice read in flight at sign-out cannot recreate the read cache', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  const pending = deferred<Response>();
  vi.stubGlobal('fetch', vi.fn(() => pending.promise));
  const inFlight = worker.get(PROFILE);
  await new Promise((resolve) => setTimeout(resolve, 10));
  await worker.message({ type: 'URY_END_SESSION' });
  pending.resolve(new Response('Alice response started before sign-out', { status: 200 }));
  await inFlight;
  expect(await storage.keys()).toEqual([]);
});

test('Alice -> sign-out -> Bob: an outage never shows Bob Alice data', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  vi.stubGlobal('fetch', ok('Alice profile'));
  await worker.get(PROFILE);
  expect(await storage.body('ury-pos-reads', PROFILE)).toBe('Alice profile');
  await worker.message({ type: 'URY_END_SESSION' });
  await worker.message({ type: 'URY_BIND_SESSION', user: 'bob@ury.com' });
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
});

test('binding a different user without sign-out drops the previous user data', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  vi.stubGlobal('fetch', ok('Alice profile'));
  await worker.get(PROFILE);
  await worker.message({ type: 'URY_BIND_SESSION', user: 'bob@ury.com' });
  expect(await storage.body('ury-pos-reads', PROFILE)).toBeUndefined();
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
});

test('an unbound worker neither stores nor serves reads', async () => {
  await (await storage.open('ury-pos-reads')).put(PROFILE, new Response('stale unowned profile'));
  const worker = await startWorker();
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
  vi.stubGlobal('fetch', ok('fresh'));
  expect(await (await worker.get(PROFILE)).text()).toBe('fresh');
  expect(await storage.body('ury-pos-reads', PROFILE)).toBe('stale unowned profile');
});

test('the bound user survives a worker restart, so an offline reload still works', async () => {
  const first = await startWorker();
  await first.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  vi.stubGlobal('fetch', ok('Alice profile'));
  await first.get(PROFILE);
  vi.resetModules();
  const restarted = await startWorker();
  vi.stubGlobal('fetch', outage());
  expect(await (await restarted.get(PROFILE)).text()).toBe('Alice profile');
});

test('Guest or empty users are never bound', async () => {
  const worker = await startWorker();
  for (const user of ['Guest', '', null]) await worker.message({ type: 'URY_BIND_SESSION', user });
  vi.stubGlobal('fetch', ok('guest page'));
  await worker.get(PROFILE);
  expect(await storage.body('ury-pos-reads', PROFILE)).toBeUndefined();
});

// Review #40 P1, race 1: the page stops waiting for the bind ack after 2 s
// while the worker is still deleting Alice's reads, then reads offline.
test('a bind still purging Alice when the page stops waiting serves Bob nothing from her cache', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  vi.stubGlobal('fetch', ok('Alice profile'));
  await worker.get(PROFILE);
  setCookieUser('bob@ury.com');
  const purge = deferred<void>();
  const removeCache = storage.delete.bind(storage);
  storage.delete = async (name: string) => {
    await purge.promise;
    return removeCache(name);
  };
  const bind = worker.message({ type: 'URY_BIND_SESSION', user: 'bob@ury.com' });
  await new Promise((resolve) => setTimeout(resolve, 10));
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
  purge.resolve();
  await bind;
});

// Review #40 P1, race 2: Bob's bind went to the redundant worker and failed;
// the replacement restores Alice's persisted session.
test('a replacement worker that never got Bob bind neither serves nor stores across users', async () => {
  const first = await startWorker();
  await first.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  vi.stubGlobal('fetch', ok('Alice profile'));
  await first.get(PROFILE);
  setCookieUser('bob@ury.com');
  vi.resetModules();
  const replacement = await startWorker();
  vi.stubGlobal('fetch', outage());
  await expect(replacement.get(PROFILE)).rejects.toThrow();
  vi.stubGlobal('fetch', ok('Bob profile'));
  expect(await (await replacement.get(PROFILE)).text()).toBe('Bob profile');
  expect(await storage.body('ury-pos-reads', PROFILE)).toBe('Alice profile');
});

test('without a signed-in user cookie the worker serves nothing from cache', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  vi.stubGlobal('fetch', ok('Alice profile'));
  await worker.get(PROFILE);
  setCookieUser(null);
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
});

test('a cached entry without an owner tag is never served', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  await (await storage.open('ury-pos-reads')).put(PROFILE, new Response('untagged profile', { status: 200 }));
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
});

test('the owner tag must name the bound user', async () => {
  const worker = await startWorker();
  await worker.message({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
  await (await storage.open('ury-pos-reads')).put(PROFILE, ownedResponse('Bob profile', 'bob@ury.com'));
  vi.stubGlobal('fetch', outage());
  await expect(worker.get(PROFILE)).rejects.toThrow();
});
