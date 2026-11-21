export const storage = {
  savePosProfileFull: (profile: unknown) => {
    localStorage.setItem('pos_profile', JSON.stringify(profile));
  },

  getPosProfileFull: () => {
    const profile = localStorage.getItem('pos_profile');
    return profile ? JSON.parse(profile) : null;
  },

  setItem: (key: string, value: string) => {
    localStorage.setItem(key, value);
  },

  getItem: (key: string): string | null => {
    return localStorage.getItem(key);
  },

  removeItem: (key: string) => {
    localStorage.removeItem(key);
  }
}; 

// The POS offline worker owns only the /pos scope and ury-pos-* caches.
// Desk, login, Serve (/ury/serve/) and other apps' workers and caches survive a purge.
export const URY_POS_SCOPE = '/pos';
export const URY_POS_CACHE_PREFIX = 'ury-pos-';
// Per-user sessionStorage prefixes the offline cashier writes; purged with the session.
const OFFLINE_SESSION_PREFIXES = ['ury_open_table_checks', 'ury_open_table_checks_session', 'ury_offline_opening', 'posProfile', 'payment_modes'];

export function getUserSessionStorageKey(key: string, user: string | null | undefined): string | null {
  return user && user !== 'Guest' ? `${key}:${user}` : null;
}

export const getSessionUser = (): string | null => {
  if (typeof document === 'undefined') return null;
  const cookie = document.cookie.split(';').map((part) => part.trim())
    .find((part) => part.startsWith('user_id='));
  if (!cookie) return null;
  try {
    return decodeURIComponent(cookie.slice('user_id='.length)) || null;
  } catch {
    return null;
  }
};

export function sessionStorageKey(key: string): string {
  return getUserSessionStorageKey(key, getSessionUser()) ?? key;
}

export function isUryPosRegistration(registration: { scope: string }): boolean {
  try {
    return new URL(registration.scope).pathname === URY_POS_SCOPE;
  } catch {
    return false;
  }
}

const WORKER_ACK_TIMEOUT_MS = 2000;

// Post a message and wait (bounded) for the worker's ack on a MessageChannel.
export function tellOfflineWorker(
  worker: { postMessage(message: unknown, transfer: Transferable[]): void } | null | undefined,
  message: unknown,
): Promise<void> {
  if (!worker || typeof MessageChannel === 'undefined') return Promise.resolve();
  return new Promise<void>((resolve) => {
    const channel = new MessageChannel();
    const finish = () => {
      clearTimeout(timer);
      channel.port1.close();
      resolve();
    };
    const timer = setTimeout(finish, WORKER_ACK_TIMEOUT_MS);
    channel.port1.onmessage = finish;
    try {
      worker.postMessage(message, [channel.port2]);
    } catch {
      finish();
    }
  });
}

async function settle(step: () => Promise<unknown>): Promise<void> {
  try {
    await step();
  } catch (error) {
    console.error('Offline cache purge step failed', error);
  }
}

// Ends the POS worker session, deletes ury-pos-* caches, unregisters only the
// /pos worker and drops the offline per-user session keys. Never rejects.
export async function purgeOfflineSession(): Promise<void> {
  if (typeof sessionStorage !== 'undefined') {
    for (let index = sessionStorage.length - 1; index >= 0; index--) {
      const key = sessionStorage.key(index);
      if (key && OFFLINE_SESSION_PREFIXES.some((prefix) => key.startsWith(`${prefix}:`))) sessionStorage.removeItem(key);
    }
  }
  let registrations: ServiceWorkerRegistration[] = [];
  await settle(async () => {
    if (typeof navigator !== 'undefined' && navigator.serviceWorker?.getRegistrations) {
      registrations = (await navigator.serviceWorker.getRegistrations()).filter(isUryPosRegistration);
    }
  });
  await Promise.all(registrations.map((registration) => settle(
    () => tellOfflineWorker(registration.active, { type: 'URY_END_SESSION' }),
  )));
  await settle(async () => {
    if (typeof caches === 'undefined' || !caches?.keys) return;
    const names = (await caches.keys()).filter((name) => name.startsWith(URY_POS_CACHE_PREFIX));
    await Promise.all(names.map((name) => caches.delete(name)));
  });
  await Promise.all(registrations.map((registration) => settle(() => registration.unregister())));
}
