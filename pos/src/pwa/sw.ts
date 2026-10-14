import { clientsClaim, setCacheNameDetails } from "workbox-core";
import { precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { createOfflineWorker } from "./offline-sw-core";

declare const self: {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>;
  location: Location;
  skipWaiting(): Promise<void>;
  addEventListener(type: "install", listener: () => void): void;
  addEventListener(
    type: "message",
    listener: (event: { data: unknown; ports: readonly MessagePort[]; waitUntil(promise: Promise<unknown>): void }) => void,
  ): void;
};

// Precache is ury-pos-precache-v2-<scope>; logout and flag-off purge ury-pos-*.
setCacheNameDetails({ prefix: 'ury-pos' });
self.addEventListener("install", () => {
  void self.skipWaiting();
});
clientsClaim();
precacheAndRoute(self.__WB_MANIFEST);
const worker = createOfflineWorker({ origin: self.location.origin });
for (const route of worker.routes) registerRoute(route);
// URY_BIND_SESSION / URY_END_SESSION from the POS page; ack on the port.
self.addEventListener("message", (event) => {
  event.waitUntil(worker.handleMessage(event.data).finally(() => event.ports[0]?.postMessage({ ok: true })));
});
