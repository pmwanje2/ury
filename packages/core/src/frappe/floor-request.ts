// Installed by the POS only. Other frontends share the SDK without a cashier binding.
export const FLOOR_USER_HEADER = 'X-URY-Floor-User';
const MISMATCH = 'URY_FLOOR_SESSION_MISMATCH';

interface Admission {
  user: string;
  lock: () => void;
  active: boolean;
}

let admission: Admission | undefined;

export function bindFloorRequestUser(user: string, lock: () => void): () => void {
  const bound = { user, lock, active: true };
  admission = bound;
  // A deferred callback from the unmounted workspace must still fail closed.
  return () => { bound.active = false; };
}

export function disposeFloorRequests(): void {
  admission = undefined;
}

export function sdkFloorRequestUser(path: string | undefined): string | undefined {
  if (admission && !admission.active && (
    path === '/api/method/login' || path === '/api/method/ury.ury_pos.floor_pin.sign_in'
    || path === '/api/method/frappe.auth.get_logged_user'
  )) return; // The locked sign-in form must be able to establish and verify a replacement session.
  return floorRequestUser();
}

export function floorRequestUser(expected = admission?.user): string | undefined {
  if (!expected) return;
  const cookie = document.cookie.split(';').map(part => part.trim()).find(part => part.startsWith('user_id='));
  let live: string | undefined;
  if (cookie !== undefined) {
    try { live = decodeURIComponent(cookie.slice('user_id='.length)); }
    catch { live = ''; }
  }
  if ((live !== undefined && live !== expected) || (admission && (!admission.active || admission.user !== expected))) {
    if (admission?.active) {
      admission.active = false;
      admission.lock();
    }
    throw new Error(MISMATCH);
  }
  return expected;
}

export function rejectFloorRequest(user: string): never {
  if (admission?.user === user && admission.active) {
    admission.active = false;
    admission.lock();
  }
  throw new Error(MISMATCH);
}

export async function floorFetch(url: string, init?: RequestInit, expected?: string): Promise<Response> {
  if ((!admission && !expected) || new URL(url, window.location.origin).origin !== window.location.origin) return fetch(url, init);
  const user = floorRequestUser(expected);
  if (!user) return fetch(url, init);
  const headers = new Headers(init?.headers);
  headers.set(FLOOR_USER_HEADER, user);
  const response = await fetch(url, { ...init, headers });
  if (response.headers.get('X-URY-Floor-Error') === MISMATCH) rejectFloorRequest(user);
  return response;
}

// Browser print windows cannot supply request headers on navigation.
export function floorPrintUrl(url: string): string {
  const user = floorRequestUser();
  if (!user) return url;
  const target = new URL(url, window.location.href);
  if (target.origin !== window.location.origin) throw new Error('Invalid print destination');
  target.searchParams.set('ury_floor_print_user', user);
  return target.href;
}
