import { withEmptyOutbox } from '../offline/outbox';
import { showToast } from '@ury/ui';
import { v4 as uuid } from 'uuid';
import { useEffect } from 'react';
import { disconnectQzPrinter, getSessionUser, logout, watchIdle } from '@ury/core';

// Tab marker: this tab signed in through the floor pad (PIN or username).
const FLOOR_SESSION_MARKER = 'userAuth';
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

const RECOVERY_MARKER = 'ury_floor_reconcile';

function currentTabId(): string {
  if (!/^ury-floor-tab:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(window.name)) {
    window.name = 'ury-floor-tab:' + uuid();
  }
  return window.name;
}

function confirmedInTab(key: string, user: string): boolean {
  try {
    const record = JSON.parse(sessionStorage.getItem(key) || 'null');
    return !!user && record?.user === user && record.tabId === currentTabId();
  } catch {
    return false;
  }
}

export function hasFloorSessionMarker(user: string): boolean {
  return confirmedInTab(FLOOR_SESSION_MARKER, user);
}

export function hasRecoveryConfirmation(user: string): boolean {
  return confirmedInTab(RECOVERY_MARKER, user);
}

export function setFloorSessionMarker(user: string, recovery: boolean): void {
  sessionStorage.removeItem(recovery ? FLOOR_SESSION_MARKER : RECOVERY_MARKER);
  sessionStorage.setItem(recovery ? RECOVERY_MARKER : FLOOR_SESSION_MARKER, JSON.stringify({ user, tabId: currentTabId() }));
}

export function needsFloorReconciliation(): boolean {
  try {
    return !!sessionStorage.getItem(RECOVERY_MARKER);
  } catch {
    return true;
  }
}

export function hasFloorLogoutFailureMarker(): boolean {
  try {
    if (localStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER)) return true;
    if (!sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER)) return false;
    // Migrate failures written by earlier POS bundles.
    localStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, getSessionUser() || 'true');
    return true;
  } catch {
    return true;
  }
}

export function clearFloorLogoutFailureMarker(): void {
  sessionStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER);
  localStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER);
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
export function retryFloorSignOut(onResult?: (result: SignOutResult) => void): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout>;
  const attempt = async () => {
    const result = await floorSignOut(() => !cancelled);
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
export async function floorSignOut(stillRequested?: () => boolean): Promise<SignOutResult> {
  let outcome: SignOutResult = 'cancelled';
  try {
    await withEmptyOutbox(async () => {
      if (stillRequested && !stillRequested()) return;
      outcome = await endFloorSession();
    });
    return outcome;
  } catch (error) {
    if (!stillRequested) showToast.error(error instanceof Error ? error.message : String(error));
    return 'blocked';
  }
}

async function endFloorSession(): Promise<SignOutResult> {
  const retrying = hasFloorLogoutFailureMarker();
  // Persist before the network request: the tab may close while logout is in flight.
  try {
    localStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, getSessionUser() || localStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER) || 'true');
  } catch {
    try { sessionStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, 'true'); } catch { /* Still attempt server logout. */ }
  }
  let logoutFailed = false;
  try {
    // @ury/core logout() resolves false when the server logout failed.
    logoutFailed = (await logout()) === false;
  } catch {
    logoutFailed = true;
  } finally {
    try {
      if (!logoutFailed) clearFloorLogoutFailureMarker();
    } catch {
      // A blocked marker must not interrupt sign-out.
    }
    for (const key of [...SESSION_KEYS, FLOOR_SESSION_MARKER, 'ury_floor_reconcile']) {
      try { sessionStorage.removeItem(key); } catch { /* Keep clearing accessible keys. */ }
    }
    try { localStorage.removeItem(FLOOR_SESSION_MARKER); } catch { /* Storage may be disabled. */ }
    disconnectQzPrinter();
    if (!retrying || !logoutFailed) window.location.replace(FLOOR_HOME);
  }
  return logoutFailed ? 'logout_failed' : 'signed_out';
}
