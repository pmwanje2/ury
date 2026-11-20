import { useEffect } from 'react';
import { disconnectQzPrinter, logout, watchIdle } from '@ury/core';

// Tab marker: this tab signed in through the floor pad (PIN or username).
const FLOOR_SESSION_MARKER = 'userAuth';
// Same-tab marker: the last server logout failed, so do not loop sign-outs.
const FLOOR_LOGOUT_FAILED_MARKER = 'ury_floor_logout_failed';
const FLOOR_HOME = '/pos/order';
// Cleared on sign-out: the previous user's cached profile and lookups.
const SESSION_KEYS = ['posProfile', 'payment_modes', 'menuCategories', 'customerGroups', 'territories'];

export interface FloorPadContext {
  paired: boolean;
  till_open: boolean;
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

export function hasFloorSessionMarker(): boolean {
  return localStorage.getItem(FLOOR_SESSION_MARKER) === 'true';
}

export function setFloorSessionMarker(): void {
  localStorage.setItem(FLOOR_SESSION_MARKER, 'true');
}

export function hasFloorLogoutFailureMarker(): boolean {
  return sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER) === 'true';
}

export function clearFloorLogoutFailureMarker(): void {
  sessionStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER);
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

    return watchIdle(() => {
      void floorSignOut();
    }, seconds * 1000);
  }, [profile, user]);
}

/**
 * End a shared-tablet session even when the server cannot answer, so the next
 * waiter never inherits the previous user's tab state.
 */
export async function floorSignOut(): Promise<void> {
  let logoutFailed = false;
  try {
    await logout();
  } catch {
    logoutFailed = true;
  } finally {
    if (logoutFailed) {
      sessionStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, 'true');
    } else {
      clearFloorLogoutFailureMarker();
    }
    localStorage.removeItem(FLOOR_SESSION_MARKER);
    SESSION_KEYS.forEach((key) => sessionStorage.removeItem(key));
    disconnectQzPrinter();
    window.location.replace(FLOOR_HOME);
  }
}
