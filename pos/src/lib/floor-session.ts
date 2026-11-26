import { v4 as uuid } from 'uuid';
import { readGeneration, writeGeneration, readLogoutMarker, writeLogoutMarker } from './logout-store';
import { hasWebLocks, withEmptyOutbox } from '../offline/outbox';
import { showToast } from '@ury/ui';
import { boundTabGeneration, clearTabConfirmation, observeTabGeneration, revokeTabConfirmation, setFloorSessionMarker } from './floor-tab';
export { hasFloorSessionMarker, hasRecoveryConfirmation, needsFloorReconciliation, setFloorSessionMarker } from './floor-tab';
import { useEffect } from 'react';
import { disconnectQzPrinter, getSessionUser, logout, watchIdle } from '@ury/core';

const LEGACY_FLOOR_SESSION_MARKER = 'userAuth';
// Origin-wide because all tabs share the server session cookie.
const FLOOR_LOGOUT_FAILED_MARKER = 'ury_floor_logout_failed';
const FLOOR_HOME = '/pos/order';
// Cleared on sign-out: the previous user's cached profile and lookups.
const SESSION_KEYS = ['posProfile', 'payment_modes', 'menuCategories', 'customerGroups', 'territories'];

export interface FloorPadContext {
  paired: boolean;
  till_open: boolean;
  min_length?: number;
  max_length?: number;
}

interface FloorUser {
  roles: string[];
}

interface StayRoleProfile {
  custom_floor_stay_signed_in_roles?: Array<{ role?: string }>;
  floor_idle_signout_seconds?: number;
}

declare global {
  interface Window {
    csrf_token?: string;
    floor_pad?: FloorPadContext;
  }
}

export function getFloorPad(): FloorPadContext {
  if (typeof window === 'undefined') return { paired: false, till_open: false };
  return window.floor_pad ?? { paired: false, till_open: false };
}

export function isFloorPadActive(): boolean {
  const pad = getFloorPad();
  return pad.paired === true && pad.till_open === true;
}

export function openFloorLogoutChannel(): BroadcastChannel | undefined {
  try { return new BroadcastChannel('ury-floor-logout'); } catch { return undefined; }
}

const GENERATION_KEY = 'ury_floor_generation';

function cookieValue(key: string): string {
  return document.cookie.split('; ').find(pair => pair.startsWith(key + '='))?.slice(key.length + 1) ?? '';
}

function writePosCookie(key: string, value: string): void {
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = key + '=' + value + '; Path=/pos; SameSite=Strict' + secure;
}

function writeLogoutCookie(failed: boolean): void {
  writePosCookie(FLOOR_LOGOUT_FAILED_MARKER, failed ? '1; Max-Age=34560000' : '; Max-Age=0');
}

// A read-only store can retain an older token. The logical timestamp orders the
// replicas, and the UUID distinguishes simultaneous writers without Web Locks.
function newestGeneration(durable: string | undefined): string {
  let local = '';
  try { local = localStorage.getItem(GENERATION_KEY) || ''; } catch { /* Other replicas remain readable. */ }
  let cookie = '';
  try { cookie = cookieValue(GENERATION_KEY); } catch { /* A cookie policy can disable the mirror. */ }
  return [durable || '', local, cookie].sort().pop()!;
}

export async function currentFloorGeneration(): Promise<string> {
  return newestGeneration(await readGeneration());
}

async function rotateGeneration(current: string): Promise<string> {
  const time = Math.max(Date.now(), Number(current.split(':')[0]) + 1);
  const generation = String(time).padStart(16, '0') + ':' + uuid();
  let saved = false;
  try { await writeGeneration(generation); saved = true; } catch { /* A full database can remain readable. */ }
  try { localStorage.setItem(GENERATION_KEY, generation); saved = true; } catch { /* Quota or storage policy. */ }
  try {
    writePosCookie(GENERATION_KEY, generation + '; Max-Age=34560000');
    saved ||= cookieValue(GENERATION_KEY) === generation;
  } catch { /* Cookie storage may be disabled. */ }
  if (!saved) throw new Error('Unable to persist POS session generation');
  return generation;
}

function announceSessionChange(type: 'logout' | 'login', generation?: string): void {
  const channel = openFloorLogoutChannel();
  try { channel?.postMessage({ type, generation }); } catch { /* Mounted guards also poll. */ }
  finally { channel?.close(); }
}

function hasLogoutCookie(): boolean {
  return document.cookie.split('; ').includes(FLOOR_LOGOUT_FAILED_MARKER + '=1');
}

export function hasFloorLogoutFailureMarker(): boolean {
  try {
    return !!(hasLogoutCookie() || localStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER)
      || sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER));
  } catch {
    return true;
  }
}

// A private retry survives another tab clearing the shared failure markers.
export function revokeStaleLogoutRetry(generation: string, retry = sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER)): boolean {
  if (retry === null || (generation !== '' && retry === generation)) return false;
  revokeTabConfirmation();
  sessionStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER);
  return true;
}

export async function readFloorLogoutFailureMarker(): Promise<boolean> {
  try { return await readLogoutMarker() || hasFloorLogoutFailureMarker(); } catch { return true; }
}

function clearLogoutMirrors(): void {
  try {
    if (hasLogoutCookie()) writeLogoutCookie(false);
  } finally {
    try { localStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER); }
    finally { sessionStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER); }
  }
}

export async function clearFloorLogoutFailureMarker(): Promise<void> {
  // A verified login can clear a cookie-only failure even when IndexedDB is read-only.
  if (await readLogoutMarker()) await writeLogoutMarker(null);
  clearLogoutMirrors();
}

export async function confirmVerifiedLogin(user: string, recovery: boolean): Promise<void> {
  // Publish the new generation durably before removing evidence of a failed logout.
  const generation = await rotateGeneration(await currentFloorGeneration());
  await clearFloorLogoutFailureMarker();
  setFloorSessionMarker(user, recovery, generation);
  announceSessionChange('login', generation);
}

export function isStaySignedIn(user: FloorUser, profile: unknown): boolean {
  const stayRoles = profile && typeof profile === 'object'
    ? ((profile as StayRoleProfile).custom_floor_stay_signed_in_roles ?? [])
    : [];
  return user.roles.some((role) => stayRoles.some((row) => row.role === role));
}

export function useFloorIdle(user: FloorUser | null, profile: StayRoleProfile | null): void {
  useEffect(() => {
    if (!user || !profile || !isFloorPadActive() || isStaySignedIn(user, profile)) return;

    const seconds = profile.floor_idle_signout_seconds ?? 0;
    if (seconds <= 0) return;

    let cancelSignOut: (() => void) | undefined;
    return watchIdle(
      () => { cancelSignOut = retryFloorSignOut(); },
      seconds * 1000,
      () => cancelSignOut?.(),
    );
  }, [profile, user]);
}

type SignOutResult = 'signed_out' | 'logout_failed' | 'blocked' | 'cancelled';

// Recheck under the outbox lock, including when replay ran in a different tab.
export function retryFloorSignOut(onResult?: (result: SignOutResult) => void, retryingFailedLogout = false): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout>;
  const attempt = async () => {
    const result = await floorSignOut(() => !cancelled, retryingFailedLogout);
    if (cancelled) return;
    onResult?.(result);
    if (result === 'blocked') timer = setTimeout(attempt, 5000);
  };
  void attempt();
  return () => {
    cancelled = true;
    clearTimeout(timer);
  };
}

/**
 * End a shared-tablet session even when the server cannot answer, so the next
 * waiter never inherits the previous user's tab state.
 */
export async function floorSignOut(stillRequested?: () => boolean, retryingFailedLogout = false): Promise<SignOutResult> {
  let outcome: SignOutResult = 'cancelled';
  try {
    await withEmptyOutbox(async () => {
      if (stillRequested) {
        if (retryingFailedLogout && !await readFloorLogoutFailureMarker()) {
          outcome = 'signed_out';
          return;
        }
        if (!stillRequested()) return;
      }
      outcome = await endFloorSession(retryingFailedLogout, stillRequested);
    });
    return outcome;
  } catch (error) {
    if (!stillRequested) showToast.error(error instanceof Error ? error.message : String(error));
    return 'blocked';
  }
}

function lockLogoutTab(): false {
  revokeTabConfirmation();
  window.location.replace(FLOOR_HOME);
  return false;
}

export async function logoutWithFloorMarker(stillRequested?: () => boolean): Promise<boolean | undefined> {
  // Check the caller after acquiring the same lock used by verified login.
  if (!hasWebLocks()) return lockLogoutTab();
  const bound = boundTabGeneration();
  if (bound === null) return lockLogoutTab();
  const privateRetry = sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER);

  // Without a snapshot, only a pristine origin (or a matching private retry) can log out.
  const expected = bound ?? privateRetry ?? '';
  const current = await currentFloorGeneration().catch(() =>
    expected === '' && typeof indexedDB === 'undefined' ? newestGeneration(undefined) : undefined,
  );
  if (privateRetry !== null) {
    if (current === undefined) return lockLogoutTab();
    if (revokeStaleLogoutRetry(current, privateRetry)) return false;
  }
  if (expected !== current) return lockLogoutTab();

  // Activity may cancel an idle logout while its generation is being read.
  if (stillRequested && !stillRequested()) return undefined;

  // Persist before the network request: the tab may close while logout is in flight.
  try { clearTabConfirmation(); } catch { /* Still attempt server logout. */ }
  let owner = 'true';
  try { owner = getSessionUser() || owner; } catch { /* Cookies may be inaccessible. */ }
  let localSaved = false;
  try {
    localStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, owner);
    localSaved = true;
  } catch { /* Use the generation-bound private marker when local storage is full. */ }
  try {
    await writeLogoutMarker(owner);
  } catch {
    // Both quota pools can be exhausted. Leave a POS-scoped flag and still attempt server logout.
    if (!localSaved) {
      try { writeLogoutCookie(true); }
      catch { /* Still attempt server logout if the browser also refuses cookies. */ }
    }
  }
  let generation: string | undefined;
  try { generation = await rotateGeneration(current); } catch { /* Still attempt server logout. */ }
  try {
    if (!localSaved || privateRetry !== null) {
      // An unpersisted generation cannot authorize a future server retry.
      sessionStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, generation ?? 'unbound');
    }
  } catch { /* The origin-wide failure markers still protect accessible storage. */ }
  announceSessionChange('logout', generation);
  if (await logout().catch(() => false) === false) {
    // A retry from this control belongs to the generation its own logout attempt created.
    try {
      if (generation) observeTabGeneration(generation);
    } catch { /* Without a snapshot a retry will lock locally if the origin has advanced. */ }
    return false;
  }
  // The server session has ended. Attempt each cleanup even if the durable store is unavailable.
  try { await writeLogoutMarker(null); } catch { /* A surviving marker keeps the next boot locked. */ }
  try { clearLogoutMirrors(); } catch { /* The server session has ended. */ }
  return true;
}

async function endFloorSession(retryingFailedLogout: boolean, stillRequested?: () => boolean): Promise<SignOutResult> {
  const retrying = retryingFailedLogout || hasFloorLogoutFailureMarker();
  const signedOut = await logoutWithFloorMarker(stillRequested);
  if (signedOut === undefined) return 'cancelled';
  if (!signedOut && boundTabGeneration() === null) return 'logout_failed';
  for (const key of SESSION_KEYS) {
    try { sessionStorage.removeItem(key); } catch { /* Keep clearing accessible keys. */ }
  }
  try { localStorage.removeItem(LEGACY_FLOOR_SESSION_MARKER); } catch { /* Storage may be disabled. */ }
  disconnectQzPrinter();
  if (!retrying || signedOut) window.location.replace(FLOOR_HOME);
  return signedOut ? 'signed_out' : 'logout_failed';
}
