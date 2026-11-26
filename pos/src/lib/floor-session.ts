import { readLogoutMarker, writeLogoutMarker } from './logout-store';
import { withEmptyOutbox } from '../offline/outbox';
import { showToast } from '@ury/ui';
import { clearTabConfirmation } from './floor-tab';
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

function hasLogoutCookie(): boolean {
  return document.cookie.split('; ').includes(FLOOR_LOGOUT_FAILED_MARKER + '=1');
}

export function hasFloorLogoutFailureMarker(): boolean {
  try {
    if (hasLogoutCookie() || localStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER)) return true;
    if (!sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER)) return false;
    // Migrate failures written by earlier POS bundles.
    localStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, getSessionUser() || 'true');
    return true;
  } catch {
    return true;
  }
}

export async function readFloorLogoutFailureMarker(): Promise<boolean> {
  try { return await readLogoutMarker() || hasFloorLogoutFailureMarker(); } catch { return true; }
}

export async function clearFloorLogoutFailureMarker(): Promise<void> {
  try {
    await writeLogoutMarker(null);
  } finally {
    try {
      if (hasLogoutCookie()) document.cookie = FLOOR_LOGOUT_FAILED_MARKER + '=; Max-Age=0; Path=/pos; SameSite=Strict';
    } finally {
      try { localStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER); }
      finally { sessionStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER); }
    }
  }
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
      if (stillRequested && !stillRequested()) return;
      outcome = await endFloorSession(retryingFailedLogout);
    });
    return outcome;
  } catch (error) {
    if (!stillRequested) showToast.error(error instanceof Error ? error.message : String(error));
    return 'blocked';
  }
}

export async function logoutWithFloorMarker(): Promise<boolean> {
  // Persist before the network request: the tab may close while logout is in flight.
  try { clearTabConfirmation(); } catch { /* Still attempt server logout. */ }
  let owner = 'true';
  try { owner = getSessionUser() || owner; } catch { /* Cookies may be inaccessible. */ }
  let localSaved = false;
  try {
    localStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, owner);
    localSaved = true;
  } catch {
    try { sessionStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, 'true'); } catch { /* Still attempt server logout. */ }
  }
  try {
    await writeLogoutMarker(owner);
  } catch {
    // Both quota pools can be exhausted. Leave a POS-scoped flag and still attempt server logout.
    if (!localSaved) {
      try { document.cookie = FLOOR_LOGOUT_FAILED_MARKER + '=1; Max-Age=34560000; Path=/pos; SameSite=Strict'; }
      catch { /* Still attempt server logout if the browser also refuses cookies. */ }
    }
  }
  try {
    if (await logout() === false) return false;
  } catch {
    return false;
  }
  try { await clearFloorLogoutFailureMarker(); } catch { /* The server session has ended. */ }
  return true;
}

async function endFloorSession(retryingFailedLogout: boolean): Promise<SignOutResult> {
  const retrying = retryingFailedLogout || hasFloorLogoutFailureMarker();
  const signedOut = await logoutWithFloorMarker();
  for (const key of SESSION_KEYS) {
    try { sessionStorage.removeItem(key); } catch { /* Keep clearing accessible keys. */ }
  }
  try { localStorage.removeItem(LEGACY_FLOOR_SESSION_MARKER); } catch { /* Storage may be disabled. */ }
  disconnectQzPrinter();
  if (!retrying || signedOut) window.location.replace(FLOOR_HOME);
  return signedOut ? 'signed_out' : 'logout_failed';
}
