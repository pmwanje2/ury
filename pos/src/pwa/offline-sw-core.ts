import { Route } from "workbox-routing";
import { NetworkFirst } from "workbox-strategies";
import type { WorkboxPlugin } from "workbox-core/types.js";
import { matchOfflineRequest, READ_CACHE, SHELL_CACHE } from "./runtime-caching";

export const SESSION_CACHE = "ury-pos-session";
export const BIND_SESSION = "URY_BIND_SESSION";
export const END_SESSION = "URY_END_SESSION";

export interface OfflineWorkerOptions {
  origin: string;
}

const MAX_ENTRIES: Record<string, number> = { [READ_CACHE]: 60, [SHELL_CACHE]: 8 };

// A 401/403 or a session change drops every cached read and shell.
export async function purgeSessionCaches(): Promise<void> {
  await Promise.all([READ_CACHE, SHELL_CACHE].map((name) => self.caches.delete(name)));
}

// Keeps each runtime cache bounded without IndexedDB: oldest keys go first.
function trimPlugin(cacheName: string): WorkboxPlugin {
  return {
    cacheDidUpdate: async () => {
      const cache = await self.caches.open(cacheName);
      const keys = await cache.keys();
      const excess = keys.length - MAX_ENTRIES[cacheName];
      for (let index = 0; index < excess; index += 1) await cache.delete(keys[index]);
    },
  };
}

function usableUser(value: unknown): string | null {
  return typeof value === "string" && value !== "" && value !== "Guest" ? value : null;
}

export function createOfflineWorker({ origin }: OfflineWorkerOptions) {
  const sessionKey = origin + "/__ury_pos_offline_session";
  // generation changes on every sign-out, user change and 401/403. A request
  // only reads or writes the cache if its generation is still current, so a
  // response that was in flight at sign-out cannot recreate the cache.
  const session = { generation: 0, user: null as string | null };

  const ready = (async () => {
    const stored = await self.caches.match(sessionKey, { cacheName: SESSION_CACHE });
    if (stored) session.user = usableUser((await stored.json())?.user);
  })().catch(() => undefined);

  async function endSession(): Promise<void> {
    session.generation += 1;
    session.user = null;
    await purgeSessionCaches();
    await self.caches.delete(SESSION_CACHE);
  }

  async function bindSession(value: unknown): Promise<void> {
    const user = usableUser(value);
    if (!user || user === session.user) return;
    session.generation += 1;
    session.user = user;
    await purgeSessionCaches();
    const cache = await self.caches.open(SESSION_CACHE);
    await cache.put(sessionKey, new Response(JSON.stringify({ user })));
  }

  const sessionPlugin: WorkboxPlugin = {
    handlerWillStart: async ({ state }) => {
      await ready;
      if (state) {
        state.generation = session.generation;
        state.user = session.user;
      }
    },
    cachedResponseWillBeUsed: async ({ cachedResponse, state }) => (
      state?.user && state.generation === session.generation ? cachedResponse ?? null : null
    ),
    cacheWillUpdate: async ({ response, state }) => (
      response.status === 200 && state?.user && state.generation === session.generation ? response : null
    ),
    cacheDidUpdate: async ({ cacheName, state }) => {
      // Sign-out landed between the check above and the write.
      if (state?.generation !== session.generation) await self.caches.delete(cacheName);
    },
    fetchDidSucceed: async ({ response }) => {
      // The server answered for this session: never serve its cache again.
      if (response.status === 401 || response.status === 403) await endSession();
      return response;
    },
  };

  // No networkTimeoutSeconds: a timeout would race the cache against a slow
  // 401/403. The cache is used only when the fetch rejects (no HTTP answer).
  const routes = [READ_CACHE, SHELL_CACHE].map((cacheName) => new Route(
    ({ url, request }) => matchOfflineRequest(url, request.method, origin) === cacheName,
    new NetworkFirst({ cacheName, plugins: [sessionPlugin, trimPlugin(cacheName)] }),
    "GET",
  ));

  async function handleMessage(data: unknown): Promise<void> {
    await ready;
    const message = data as { type?: unknown; user?: unknown } | null;
    if (message?.type === END_SESSION) await endSession();
    else if (message?.type === BIND_SESSION) await bindSession(message.user);
  }

  return { routes, handleMessage, ready };
}
