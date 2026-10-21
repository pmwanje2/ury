import { useEffect } from 'react';
import {
  clearAppStorage,
  disconnectQzPrinter,
  watchIdle,
} from '@ury/core';
import { clearCashierStorage } from '../offline/cashier';

const FLOOR_SESSION_MARKER = 'userAuth';
const FLOOR_LOGOUT_FAILED_MARKER = 'ury_floor_logout_failed';

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
  return typeof localStorage !== 'undefined' && localStorage.getItem(FLOOR_SESSION_MARKER) === 'true';
}

export function setFloorSessionMarker(): void {
  localStorage.setItem(FLOOR_SESSION_MARKER, 'true');
}

export function clearFloorSessionMarker(): void {
  if (typeof localStorage !== 'undefined') localStorage.removeItem(FLOOR_SESSION_MARKER);
}

export function hasFloorLogoutFailureMarker(): boolean {
  return typeof sessionStorage !== 'undefined'
    && sessionStorage.getItem(FLOOR_LOGOUT_FAILED_MARKER) === 'true';
}

export function clearFloorLogoutFailureMarker(): void {
  if (typeof sessionStorage !== 'undefined') sessionStorage.removeItem(FLOOR_LOGOUT_FAILED_MARKER);
}

export function isStaySignedIn(user: FloorUser, profile: unknown): boolean {
  const stayRoles = profile && typeof profile === 'object' && 'custom_floor_stay_signed_in_roles' in profile
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
 * End a shared-tablet session even when the server cannot answer. The waiter
 * floor flow has no outbox, so the existing cashier cleanup seam is safe and
 * keeps the local tab from retaining the previous user's state.
 */
export async function floorSignOut(): Promise<void> {
  let logoutFailed = false;
  try {
    await clearCashierStorage(true, window.csrf_token);
  } catch {
    logoutFailed = true;
    try {
      await clearAppStorage();
    } catch {
      // Local marker and printer state are still cleared below.
    }
  } finally {
    if (logoutFailed) {
      sessionStorage.setItem(FLOOR_LOGOUT_FAILED_MARKER, 'true');
    } else {
      clearFloorLogoutFailureMarker();
    }
    clearFloorSessionMarker();
    disconnectQzPrinter();
    window.location.replace('/pos/order');
  }
}
