import { Route } from "workbox-routing";
import { NetworkFirst } from "workbox-strategies";
import type { WorkboxPlugin } from "workbox-core/types.js";
import { matchOfflineRequest, READ_CACHE, SHELL_CACHE } from "./runtime-caching";
import { isGatewayUnreachableStatus } from "../offline/gateway";

export const SESSION_CACHE = "ury-pos-session";
export const BIND_SESSION = "URY_BIND_SESSION";
export const END_SESSION = "URY_END_SESSION";
export const OWNER_HEADER = "X-Ury-Pos-Owner";

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

// The browser's signed-in user from Frappe's user_id cookie. Unreadable or
// absent means no user, and then nothing is served from or stored in cache.
async function signedInUser(): Promise<string | null> {
  const store = (self as unknown as { cookieStore?: { get(name: string): Promise<{ value?: string } | null> } }).cookieStore;
  try {
    const cookie = await store?.get("user_id");
    return cookie?.value ? usableUser(decodeURIComponent(cookie.value)) : null;
  } catch {
    return null;
  }
}

function ownedBy(response: Response, owner: string): Response {
  const headers = new Headers(response.headers);
  headers.set(OWNER_HEADER, owner);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
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
      const user = await signedInUser();
      if (state) {
        state.generation = session.generation;
        // Serve and store only for the signed-in user, and store only once the
        // worker is bound to that same user. Client timing cannot widen this.
        state.user = user;
        state.store = user !== null && user === session.user;
      }
    },
    cachedResponseWillBeUsed: async ({ cachedResponse, state }) => (
      cachedResponse && state?.user && state.generation === session.generation
        && cachedResponse.headers.get(OWNER_HEADER) === state.user ? cachedResponse : null
    ),
    cacheWillUpdate: async ({ response, state }) => (
      response.status === 200 && state?.store && state.generation === session.generation
        ? ownedBy(response, state.user as string) : null
    ),
    cacheDidUpdate: async ({ cacheName, state }) => {
      // Sign-out landed between the check above and the write.
      if (state?.generation !== session.generation) await self.caches.delete(cacheName);
    },
    fetchDidSucceed: async ({ response }) => {
      // The server answered for this session: never serve its cache again.
      if (response.status === 401 || response.status === 403) await endSession();
      // A live proxy can answer 502/503/504/520-524 while the ERP is down.
      // That is the same as fetch rejecting. 4xx and 500 stay the answer.
      if (isGatewayUnreachableStatus(response.status)) {
        throw new TypeError(`Gateway unreachable (${response.status})`);
      }
      return response;
    },
  };

  // No networkTimeoutSeconds: a timeout would race the cache against a slow
  // 401/403. The cache is used when fetch rejects, and when the proxy answers
  // 502/503/504/520-524. Any other HTTP status is returned as-is.
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
