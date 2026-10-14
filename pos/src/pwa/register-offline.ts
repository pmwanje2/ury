import { purgeOfflineCaches } from "@ury/core";
import { offlineCaptureEnabled, type OfflineCaptureProfile } from "./runtime-caching";

const SERVICE_WORKER_URL = "/sw.js";
const SERVICE_WORKER_SCOPE = "/pos";

export async function syncOfflineCapture(profile: OfflineCaptureProfile | null | undefined): Promise<void> {
  if (!offlineCaptureEnabled(profile)) {
    await purgeOfflineCaches();
    return;
  }
  if (typeof navigator === "undefined" || !navigator.serviceWorker?.register) return;

  const serviceWorker = navigator.serviceWorker;
  await serviceWorker.register(SERVICE_WORKER_URL, { scope: SERVICE_WORKER_SCOPE });
  // serviceWorker.ready never rejects, and a failed install (for example a
  // precache 404) leaves it pending. Do not wait on it or the till stays on
  // the loader. controllerchange below is already bounded.
  if (!serviceWorker.controller) {
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, 2000);
      serviceWorker.addEventListener("controllerchange", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }
  if (!serviceWorker.controller || typeof fetch !== "function") return;
  try {
    await fetch("/pos", { credentials: "same-origin", cache: "reload" });
  } catch {
    // A failed warm-up must not block the till. The next controlled load caches the shell.
  }
}

// True when the URY POS worker controls this page. It only registers while
// the till's ury_offline_capture flag is on; flag off unregisters it.
export function offlineShellActive(): boolean {
  const controller = typeof navigator !== "undefined" ? navigator.serviceWorker?.controller : null;
  if (!controller) return false;
  try {
    return new URL(controller.scriptURL).pathname === SERVICE_WORKER_URL;
  } catch {
    return false;
  }
}
