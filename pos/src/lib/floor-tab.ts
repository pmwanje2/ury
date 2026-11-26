import { v4 as uuid } from 'uuid';

const FLOOR_SESSION_MARKER = 'userAuth';
const RECOVERY_MARKER = 'ury_floor_reconcile';
const OBSERVED_GENERATION = 'ury_floor_observed_generation';

// window.name belongs to this browsing context and survives same-tab navigation,
// even when the browser refuses every sessionStorage write.
const LOCKED_TAB_NAME = 'ury-floor-locked';
export const FLOOR_TAB_LOCKED = 'pos:floor-tab-locked';

function currentTabId(): string {
  if (window.name === LOCKED_TAB_NAME) return LOCKED_TAB_NAME;
  if (!/^ury-floor-tab:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(window.name)) {
    window.name = 'ury-floor-tab:' + uuid();
  }
  return window.name;
}

const REVOKED = 'revoked';

// An observed generation protects logout but never grants PIN or recovery confirmation.
export function observeTabGeneration(generation: string): void {
  sessionStorage.setItem(OBSERVED_GENERATION, JSON.stringify({ tabId: currentTabId(), generation }));
}

// undefined means no binding; null means revoked or invalid. An empty string binds the initial generation.
export function boundTabGeneration(): string | null | undefined {
  if (window.name === LOCKED_TAB_NAME) return null;
  for (const key of [FLOOR_SESSION_MARKER, RECOVERY_MARKER, OBSERVED_GENERATION]) {
    const value = sessionStorage.getItem(key);
    if (value === REVOKED) return null;
    if (!value?.startsWith('{')) continue;
    try {
      const record = JSON.parse(value);
      if (record.tabId === currentTabId()) return typeof record.generation === 'string' ? record.generation : null;
    } catch {
      return null;
    }
  }
  return undefined;
}

export function revokeTabConfirmation(): void {
  try {
    sessionStorage.setItem(FLOOR_SESSION_MARKER, REVOKED);
    sessionStorage.removeItem(OBSERVED_GENERATION);
    sessionStorage.removeItem(RECOVERY_MARKER);
  } catch {
    window.name = LOCKED_TAB_NAME;
    try {
      clearTabConfirmation();
      sessionStorage.setItem(FLOOR_SESSION_MARKER, REVOKED);
    } catch { /* The browsing-context marker keeps this tab locked. */ }
  }
}

function confirmedInTab(key: string, user: string): boolean {
  if (window.name === LOCKED_TAB_NAME) return false;
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
  sessionStorage.removeItem(OBSERVED_GENERATION);
  sessionStorage.removeItem(recovery ? FLOOR_SESSION_MARKER : RECOVERY_MARKER);
  const tabId = window.name === LOCKED_TAB_NAME ? 'ury-floor-tab:' + uuid() : currentTabId();
  sessionStorage.setItem(recovery ? RECOVERY_MARKER : FLOOR_SESSION_MARKER, JSON.stringify({ user, tabId, generation }));
  window.name = tabId;
}

export function clearTabConfirmation(): void {
  sessionStorage.removeItem(OBSERVED_GENERATION);
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
