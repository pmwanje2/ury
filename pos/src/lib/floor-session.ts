import {
  clearAppStorage,
  disconnectQzPrinter,
} from '@ury/core';
import { clearCashierStorage } from '../offline/cashier';

const FLOOR_SESSION_MARKER = 'userAuth';

export interface FloorPadContext {
  paired: boolean;
  till_open: boolean;
}

interface FloorUser {
  roles: string[];
}

interface StayRoleProfile {
  [key: string]: unknown;
  custom_floor_stay_signed_in_roles?: Array<{ role?: string }>;
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

export function isStaySignedIn(user: FloorUser, profile: unknown): boolean {
  const stayRoles = profile && typeof profile === 'object' && 'custom_floor_stay_signed_in_roles' in profile
    ? ((profile as StayRoleProfile).custom_floor_stay_signed_in_roles ?? [])
    : [];
  return user.roles.some((role) => stayRoles.some((row) => row.role === role));
}

/**
 * End a shared-tablet session even when the server cannot answer. The waiter
 * floor flow has no outbox, so the existing cashier cleanup seam is safe and
 * keeps the local tab from retaining the previous user's state.
 */
export async function floorSignOut(): Promise<void> {
  try {
    await clearCashierStorage(true, window.csrf_token);
  } catch {
    try {
      await clearAppStorage();
    } catch {
      // Local marker and printer state are still cleared below.
    }
  } finally {
    clearFloorSessionMarker();
    sessionStorage.removeItem('ury_floor_pin_session');
    disconnectQzPrinter();
    window.location.replace('/pos/order');
  }
}
