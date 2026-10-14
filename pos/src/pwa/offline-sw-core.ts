import { Route } from "workbox-routing";
import { NetworkFirst } from "workbox-strategies";
import type { WorkboxPlugin } from "workbox-core/types.js";
import { matchOfflineRequest, READ_CACHE, SHELL_CACHE } from "./runtime-caching";

export interface OfflineWorkerOptions {
  origin: string;
}

const MAX_ENTRIES: Record<string, number> = { [READ_CACHE]: 60, [SHELL_CACHE]: 8 };

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

function strategy(cacheName: string): NetworkFirst {
  return new NetworkFirst({
    cacheName,
    networkTimeoutSeconds: 3,
    plugins: [trimPlugin(cacheName)],
  });
}

export function createOfflineWorker({ origin }: OfflineWorkerOptions) {
  const routes = [READ_CACHE, SHELL_CACHE].map((cacheName) => new Route(
    ({ url, request }) => matchOfflineRequest(url, request.method, origin) === cacheName,
    strategy(cacheName),
    "GET",
  ));
  return { routes };
}
