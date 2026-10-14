const APP_LOCAL_STORAGE_KEYS = [
  'userAuth', 'selectedRoom', 'posOrderTabsData', 'pos_profile',
  'currency', 'currencySymbol', 'kot_time', 'ury_active_branch_id',
];
const APP_SESSION_STORAGE_KEYS = [
  'posProfile', 'menuCategories', 'customerGroups', 'territories', 'payment_modes',
  'ury.setup.configureState',
];
const USER_SESSION_STORAGE_KEYS = ['posProfile', 'payment_modes', 'customerGroups', 'territories'];

export function getOrderTabsStorageKey(user: string | undefined): string | null {
  return user && user !== 'Guest' ? `posOrderTabsData:${user}` : null;
}

export function getUserSessionStorageKey(key: string, user: string | null | undefined): string | null {
  return user && user !== 'Guest' ? `${key}:${user}` : null;
}

// The POS offline worker owns only the /pos scope and ury-pos-* caches.
// Desk, login and other apps' workers and caches must survive a purge.
export const URY_POS_SCOPE = '/pos';
export const URY_POS_CACHE_PREFIX = 'ury-pos-';

export function isUryPosRegistration(registration: { scope: string }): boolean {
  try {
    return new URL(registration.scope).pathname === URY_POS_SCOPE;
  } catch {
    return false;
  }
}

export async function purgeOfflineCaches(): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.serviceWorker?.getRegistrations) {
    const registrations = (await navigator.serviceWorker.getRegistrations()).filter(isUryPosRegistration);
    await Promise.all(registrations.map((registration) => registration.unregister()));
  }
  if (typeof caches === "undefined" || !caches?.keys) return;
  const names = (await caches.keys()).filter((name) => name.startsWith(URY_POS_CACHE_PREFIX));
  await Promise.all(names.map((name) => caches.delete(name)));
}

export function clearAppStorage(): void {
  APP_LOCAL_STORAGE_KEYS.forEach((key) => localStorage.removeItem(key));
  APP_SESSION_STORAGE_KEYS.forEach((key) => sessionStorage.removeItem(key));
  // Walk backwards because removeItem changes the storage indices.
  for (let index = localStorage.length - 1; index >= 0; index--) {
    const key = localStorage.key(index);
    if (key && (key.startsWith('posOrderTabsData:') || /^.+_.+_strike$/.test(key))) {
      localStorage.removeItem(key);
    }
  }
  for (let index = sessionStorage.length - 1; index >= 0; index--) {
    const key = sessionStorage.key(index);
    if (key && (
      USER_SESSION_STORAGE_KEYS.some((prefix) => key.startsWith(`${prefix}:`)) ||
      key.startsWith('ury_rooms_') || key.startsWith('ury_room_counts_')
    )) {
      sessionStorage.removeItem(key);
    }
  }
  void purgeOfflineCaches();
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
