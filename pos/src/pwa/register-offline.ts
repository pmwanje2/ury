import { purgeOfflineSession, tellOfflineWorker } from "@ury/core";
import { offlineCaptureEnabled, type OfflineCaptureProfile } from "./runtime-caching";

const SERVICE_WORKER_URL = "/ury-pos-sw.js";
const SERVICE_WORKER_SCOPE = "/pos";

let binding: { user: string | null; promise: Promise<void> } | null = null;

async function messageControllingWorker(user: string | null): Promise<void> {
  const controller = posController();
  if (!controller) return;
  await tellOfflineWorker(controller, { type: "URY_BIND_SESSION", user });
}

// Isolation does not depend on this ack: the worker serves a cached read only
// when its owner tag is the user in the user_id cookie. Waiting (bounded) lets
// the first reads be stored for the new user once the worker is bound.
export function bindOfflineUser(user?: string | null): Promise<void> {
  const key = user ?? null;
  if (binding?.user === key) return binding.promise;
  const promise = messageControllingWorker(key).finally(() => {
    if (binding?.promise === promise) binding = null;
  });
  binding = { user: key, promise };
  return promise;
}

export function cachedOfflineProfile(user: string | null) {
  if (!user || user === 'Guest') return null;
  try {
    const profile = JSON.parse(sessionStorage.getItem(`posProfile:${user}`) || 'null');
    return offlineCaptureEnabled(profile) ? profile : null;
  } catch {
    return null;
  }
}

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
  await serviceWorker.register(SERVICE_WORKER_URL, { scope: SERVICE_WORKER_SCOPE });
  // An active registration may not control this page yet. A failed install
  // must not block boot, and a different app's claim must not receive the user.
  if (!posController()) {
    await new Promise<void>((resolve) => {
      const finish = () => {
        clearTimeout(timer);
        serviceWorker.removeEventListener?.("controllerchange", onClaim);
        resolve();
      };
      const onClaim = () => { if (posController()) finish(); };
      const timer = setTimeout(finish, 2000);
      serviceWorker.addEventListener("controllerchange", onClaim);
    });
  }
  const controller = posController();
  if (!controller) return;
  await tellOfflineWorker(controller, { type: "URY_BIND_SESSION", user: user ?? null });
  if (serviceWorker.controller !== controller || typeof fetch !== "function") return;
  // Not awaited: a slow or failed warm-up must not hold boot. The next
  // controlled load caches the shell.
  void fetch("/pos", { credentials: "same-origin", cache: "reload" }).catch(() => undefined);
}

function posController(): ServiceWorker | null {
  const controller = typeof navigator !== "undefined" ? navigator.serviceWorker?.controller : null;
  return controller && controller.scriptURL === `${window.location.origin}${SERVICE_WORKER_URL}` ? controller : null;
}

// True when the URY POS worker controls this page. It only registers while
// the till's ury_offline_capture flag is on; flag off unregisters it.
export function offlineShellActive(): boolean {
  return posController() !== null;
}
