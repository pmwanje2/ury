import { getSessionUser } from '../storage';

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

export function admittedFloorRequestUser(): string | undefined {
  return admission?.active ? admission.user : undefined;
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
  if (getSessionUser() !== expected || expected === 'Guest' || (admission && (!admission.active || admission.user !== expected))) {
    rejectFloorRequest(expected);
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

export function checkFloorFailure(user: string, header?: string | null, payload?: unknown): void {
  const body = payload as { floor_error?: string; exception?: string; message?: string } | undefined;
  if (getSessionUser() !== user || user === 'Guest' || header === MISMATCH || body?.floor_error === MISMATCH
    || body?.exception?.endsWith?.(MISMATCH) || body?.message === MISMATCH || payload === MISMATCH) {
    rejectFloorRequest(user);
  }
}

export async function floorFetch(url: string, init?: RequestInit, expected?: string): Promise<Response> {
  if ((!admission && !expected) || new URL(url, window.location.origin).origin !== window.location.origin) return fetch(url, init);
  const user = floorRequestUser(expected);
  if (!user) return fetch(url, init);
  const headers = new Headers(init?.headers);
  headers.set(FLOOR_USER_HEADER, user);
  let response: Response;
  try {
    response = await fetch(url, { ...init, headers });
  } catch (error) {
    checkFloorFailure(user);
    throw error;
  }
  const header = response.headers.get('X-URY-Floor-Error');
  if (header === MISMATCH) rejectFloorRequest(user);
  if (!response.ok) {
    checkFloorFailure(user);
    if ([401, 403, 409].includes(response.status)) {
      checkFloorFailure(user, header, await response.clone().json().catch(() => undefined));
    }
  }
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

export async function postLogout(floorUser?: string): Promise<void> {
  const csrf = (window as Window & { csrf_token?: string }).csrf_token || '';
  const init = { method: 'POST', headers: { 'X-Frappe-CSRF-Token': csrf } };
  const url = '/api/method/logout';
  const response = floorUser ? await floorFetch(url, init, floorUser) : await fetch(url, init);
  if (!response.ok) throw new Error('Logout refused');
}
