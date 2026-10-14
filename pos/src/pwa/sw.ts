import { clientsClaim, setCacheNameDetails } from "workbox-core";
import { precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { createOfflineWorker } from "./offline-sw-core";

declare const self: {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>;
  location: Location;
  skipWaiting(): Promise<void>;
  addEventListener(type: string, listener: () => void): void;
};

// Precache is ury-pos-precache-v2-<scope>; logout and flag-off purge ury-pos-*.
setCacheNameDetails({ prefix: 'ury-pos' });
self.addEventListener("install", () => {
  void self.skipWaiting();
});
clientsClaim();
precacheAndRoute(self.__WB_MANIFEST);
for (const route of createOfflineWorker({ origin: self.location.origin }).routes) registerRoute(route);
