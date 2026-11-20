import { purgeOfflineSession, tellOfflineWorker } from "@ury/core";
import { offlineCaptureEnabled, type OfflineCaptureProfile } from "./runtime-caching";

const SERVICE_WORKER_URL = "/sw.js";
const SERVICE_WORKER_SCOPE = "/pos";

export async function syncOfflineCapture(
  profile: OfflineCaptureProfile | null | undefined,
  user?: string | null,
): Promise<void> {
  if (!offlineCaptureEnabled(profile)) {
    await purgeOfflineSession();
    return;
  }
  if (typeof navigator === "undefined" || !navigator.serviceWorker?.register) return;

  const serviceWorker = navigator.serviceWorker;
  const registration = await serviceWorker.register(SERVICE_WORKER_URL, { scope: SERVICE_WORKER_SCOPE });
  // The worker stores and serves reads only for the bound user; binding a
  // different user drops the previous user's cached reads first.
  const target = registration?.active ?? serviceWorker.controller;
  if (target) await tellOfflineWorker(target, { type: "URY_BIND_SESSION", user: user ?? null });
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
