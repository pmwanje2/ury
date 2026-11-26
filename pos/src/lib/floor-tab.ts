import { v4 as uuid } from 'uuid';

const FLOOR_SESSION_MARKER = 'userAuth';
const RECOVERY_MARKER = 'ury_floor_reconcile';

function currentTabId(): string {
  if (!/^ury-floor-tab:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(window.name)) {
    window.name = 'ury-floor-tab:' + uuid();
  }
  return window.name;
}

const REVOKED = 'revoked';

// undefined identifies an unconfirmed tab; null identifies a revoked or pre-generation confirmation.
export function boundTabGeneration(): string | null | undefined {
  for (const key of [FLOOR_SESSION_MARKER, RECOVERY_MARKER]) {
    const value = sessionStorage.getItem(key);
    if (value === REVOKED) return null;
    if (!value?.startsWith('{')) continue;
    const record = JSON.parse(value);
    if (record.tabId === currentTabId()) return record.generation ?? null;
  }
  return undefined;
}

export function revokeTabConfirmation(): void {
  sessionStorage.setItem(FLOOR_SESSION_MARKER, REVOKED);
  sessionStorage.removeItem(RECOVERY_MARKER);
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

export function isFloorSessionConfirmedForTab(user?: string): boolean {
  return !!user && (hasFloorSessionMarker(user) || hasRecoveryConfirmation(user));
}

export function setFloorSessionMarker(user: string, recovery: boolean, generation = ''): void {
  sessionStorage.removeItem(recovery ? FLOOR_SESSION_MARKER : RECOVERY_MARKER);
  sessionStorage.setItem(recovery ? RECOVERY_MARKER : FLOOR_SESSION_MARKER, JSON.stringify({ user, tabId: currentTabId(), generation }));
}

export function clearTabConfirmation(): void {
  sessionStorage.removeItem(FLOOR_SESSION_MARKER);
  sessionStorage.removeItem(RECOVERY_MARKER);
}

export function needsFloorReconciliation(): boolean {
  try {
    return !!sessionStorage.getItem(RECOVERY_MARKER);
  } catch {
    return true;
  }
}
