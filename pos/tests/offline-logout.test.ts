import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { deferred } from './helpers/sw-env';

const ORIGIN = 'https://ury.localhost';
let steps: string[];
let ack: ReturnType<typeof deferred<void>>;
let messages: unknown[];

function worker() {
  return {
    scriptURL: ORIGIN + '/sw.js',
    postMessage(message: { type: string }, ports: MessagePort[]) {
      messages.push(message);
      steps.push('message:' + message.type);
      void ack.promise.then(() => ports[0]?.postMessage({ ok: true }));
    },
  };
}

beforeEach(() => {
  steps = [];
  messages = [];
  ack = deferred<void>();
  const active = worker();
  const registration = (scope: string, owned: boolean) => ({
    scope: ORIGIN + scope,
    active: owned ? active : null,
    unregister: async () => {
      steps.push('unregister:' + scope);
      return true;
    },
  });
  vi.stubGlobal('navigator', {
    serviceWorker: {
      controller: active,
      register: async (_url: string, options: { scope: string }) => {
        steps.push('register:' + options.scope);
        return registration(options.scope, true);
      },
      addEventListener: () => undefined,
      getRegistrations: async () => [registration('/', false), registration('/pos', true)],
    },
  });
  vi.stubGlobal('caches', {
    keys: async () => ['desk-cache', 'ury-pos-reads', 'ury-pos-session'],
    delete: async (name: string) => {
      steps.push('delete:' + name);
      return true;
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

test('clearAppStorage resolves only after the worker ends the session, then deletes and unregisters', async () => {
  const { clearAppStorage } = await import('../../packages/core/src/storage');
  let done = false;
  const purge = clearAppStorage().then(() => {
    done = true;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(done).toBe(false);
  expect(steps).toEqual(['message:URY_END_SESSION']);
  ack.resolve();
  await purge;
  expect(steps).toEqual([
    'message:URY_END_SESSION',
    'delete:ury-pos-reads',
    'delete:ury-pos-session',
    'unregister:/pos',
  ]);
});

test('an unresponsive worker cannot hold logout forever and failures do not reject', async () => {
  vi.stubGlobal('caches', { keys: async () => { throw new Error('quota'); }, delete: async () => true });
  const { clearAppStorage } = await import('../../packages/core/src/storage');
  const started = Date.now();
  await expect(clearAppStorage()).resolves.toBeUndefined();
  expect(Date.now() - started).toBeLessThan(4000);
  expect(steps).toContain('unregister:/pos');
}, 8000);

test('core logout resolves after the purge completes', async () => {
  vi.doMock('../../packages/core/src/frappe/client', () => ({
    auth: { logout: vi.fn(async () => undefined), getLoggedInUser: vi.fn() },
    call: {}, db: {}, NETWORK_FAILURE_MARK: 'ury_network_failure',
  }));
  const { logout } = await import('../../packages/core/src/frappe/auth');
  let done = false;
  const pending = logout().then(() => {
    done = true;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(done).toBe(false);
  ack.resolve();
  await pending;
  expect(steps).toContain('unregister:/pos');
});

test('an opted-in till binds the cookie user to its worker', async () => {
  ack.resolve();
  const { syncOfflineCapture } = await import('../src/pwa/register-offline');
  await syncOfflineCapture({ ury_offline_capture: 1 }, 'alice@ury.com');
  expect(steps[0]).toBe('register:/pos');
  expect(messages).toContainEqual({ type: 'URY_BIND_SESSION', user: 'alice@ury.com' });
});
