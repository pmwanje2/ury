// Minimal service-worker globals so real Workbox 7.4.1 strategies and the
// router run in Node. Cache Storage is in memory; nothing here fakes Workbox.
export class MemoryCache {
  entries = new Map<string, Response>();
  async match(request: RequestInfo | URL) {
    const key = new Request(request as RequestInfo).url;
    const hit = this.entries.get(key);
    return hit ? hit.clone() : undefined;
  }
  async put(request: RequestInfo | URL, response: Response) {
    this.entries.set(new Request(request as RequestInfo).url, response.clone());
  }
  async delete(request: RequestInfo | URL) {
    return this.entries.delete(new Request(request as RequestInfo).url);
  }
  async keys() {
    return [...this.entries.keys()].map((url) => new Request(url));
  }
}

export class MemoryCacheStorage {
  stores = new Map<string, MemoryCache>();
  async open(name: string) {
    let cache = this.stores.get(name);
    if (!cache) {
      cache = new MemoryCache();
      this.stores.set(name, cache);
    }
    return cache;
  }
  async has(name: string) {
    return this.stores.has(name);
  }
  async delete(name: string) {
    return this.stores.delete(name);
  }
  async keys() {
    return [...this.stores.keys()];
  }
  async match(request: RequestInfo | URL, options?: { cacheName?: string }) {
    const names = options?.cacheName ? [options.cacheName] : [...this.stores.keys()];
    for (const name of names) {
      const hit = await this.stores.get(name)?.match(request);
      if (hit) return hit;
    }
    return undefined;
  }
  async body(name: string, url: string) {
    const hit = await this.stores.get(name)?.match(url);
    return hit ? hit.text() : undefined;
  }
}

export class TestExtendableEvent extends Event {
  pending: Promise<unknown>[] = [];
  waitUntil(promise: Promise<unknown>) {
    this.pending.push(promise);
  }
  async settle() {
    await Promise.allSettled(this.pending);
  }
}

// Workbox checks `options instanceof FetchEvent` when a strategy is handled.
export class TestFetchEvent extends TestExtendableEvent {}

export const ORIGIN = 'https://ury.localhost';

export const OWNER_HEADER = 'X-Ury-Pos-Owner';

// The browser's signed-in user, as the worker reads it from the user_id
// cookie through the native Cookie Store API.
let cookieUser: string | null = 'alice@ury.com';
export function setCookieUser(user: string | null) {
  cookieUser = user;
}

// A cached read stored for owner, the way the worker tags it.
export function ownedResponse(body: string, owner: string) {
  return new Response(body, { status: 200, headers: { [OWNER_HEADER]: owner } });
}

export function installWorkerGlobals() {
  const storage = new MemoryCacheStorage();
  const scope = globalThis as unknown as Record<string, unknown>;
  scope.self = globalThis;
  scope.caches = storage;
  cookieUser = 'alice@ury.com';
  scope.cookieStore = {
    get: async (name: string) => (name === 'user_id' && cookieUser ? { name, value: encodeURIComponent(cookieUser) } : null),
  };
  scope.ExtendableEvent = TestExtendableEvent;
  scope.FetchEvent = TestFetchEvent;
  scope.__WB_DISABLE_DEV_LOGS = true;
  scope.location = new URL(ORIGIN + '/pos');
  return storage;
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

