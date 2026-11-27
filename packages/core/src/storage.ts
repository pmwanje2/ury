const APP_LOCAL_STORAGE_KEYS = [
  'userAuth', 'selectedRoom', 'posOrderTabsData', 'pos_profile',
  'currency', 'currencySymbol', 'kot_time', 'ury_active_branch_id',
];
const APP_SESSION_STORAGE_KEYS = [
  'posProfile', 'menuCategories', 'customerGroups', 'territories', 'payment_modes',
  'ury.setup.configureState', 'userAuth', 'ury_floor_reconcile',
];
const USER_SESSION_STORAGE_KEYS = ['posProfile', 'payment_modes', 'customerGroups', 'territories', 'ury_offline_opening', 'ury_open_table_checks', 'ury_open_table_checks_session', 'ury_pending_bill_copy'];

// A tab may keep printing when browser storage is unavailable. Clear its fallback on logout too.
const pendingBillCopies = new Map<string, { value: string | null; persisted: boolean }>();

export function readPendingBillCopy(slot: string): string | null {
  const fallback = pendingBillCopies.get(slot);
  if (fallback && !fallback.persisted) return fallback.value;
  try {
    const value = sessionStorage.getItem(slot);
    if (value === null) pendingBillCopies.delete(slot);
    else pendingBillCopies.set(slot, { value, persisted: true });
    return value;
  } catch {
    return fallback?.value ?? null;
  }
}

export function storePendingBillCopy(slot: string, value: string | null): void {
  try {
    if (value === null) {
      sessionStorage.removeItem(slot);
      pendingBillCopies.delete(slot);
    } else {
      sessionStorage.setItem(slot, value);
      pendingBillCopies.set(slot, { value, persisted: true });
    }
  } catch {
    // A failed write lives only in this tab. A failed removal masks the old disk value.
    pendingBillCopies.set(slot, { value, persisted: false });
  }
}

function removeStorageKeys(
  area: 'localStorage' | 'sessionStorage',
  keys: readonly string[],
  matches: (key: string) => boolean,
): void {
  try {
    const store = globalThis[area];
    const remove = (key: string) => {
      try {
        store.removeItem(key);
      } catch {
        // Continue with the other keys if this removal is refused.
      }
    };
    keys.forEach(remove);
    // Removing entries changes the indices of the entries that follow them.
    for (let index = store.length - 1; index >= 0; index--) {
      const key = store.key(index);
      if (key && matches(key)) remove(key);
    }
  } catch {
    // Browser policy may prevent obtaining or enumerating this storage area.
  }
}

export function clearAppStorage({ includeMosaicStrikes = true } = {}): void {
  pendingBillCopies.clear();
  removeStorageKeys('localStorage', APP_LOCAL_STORAGE_KEYS, key =>
    key.startsWith('posOrderTabsData:') || (includeMosaicStrikes && /^.+_.+_strike$/.test(key)));
  removeStorageKeys('sessionStorage', APP_SESSION_STORAGE_KEYS, key =>
    USER_SESSION_STORAGE_KEYS.some(prefix => key.startsWith(prefix + ':')) ||
    key.startsWith('ury_rooms_') || key.startsWith('ury_room_counts_'));
}

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
  try {
    const cookie = document.cookie.split(';').map((part) => part.trim())
      .find((part) => part.startsWith('user_id='));
    return cookie ? decodeURIComponent(cookie.slice('user_id='.length)) || null : null;
  } catch {
    return null;
  }
};

export function sessionStorageKey(key: string): string | null {
  return getUserSessionStorageKey(key, getSessionUser());
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
  removeStorageKeys('sessionStorage', [], key =>
    OFFLINE_SESSION_PREFIXES.some(prefix => key.startsWith(prefix + ':')));
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

export function ownedSessionKey(key: string): string | null {
  sessionStorage.removeItem(key);
  return sessionStorageKey(key);
}
