import { getUserSessionStorageKey } from '../../../packages/core/src/storage';

// Server-known open checks for this cashier. One row per table. Logout clears
// the prefix via USER_SESSION_STORAGE_KEYS.
export interface OpenTableCheck {
  table: string;
  clientOrderId: string;
  invoice?: string;
  grandTotal?: number;
  customer?: string;
  orderType?: string;
  noOfPax?: number;
  room?: string;
}

const PREFIX = 'ury_open_table_checks';

function keyFor(user: string): string | null {
  return getUserSessionStorageKey(PREFIX, user);
}

export function readOpenTableChecks(user: string): OpenTableCheck[] {
  const key = keyFor(user);
  if (!key || typeof sessionStorage === 'undefined') return [];
  try {
    const parsed = JSON.parse(sessionStorage.getItem(key) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((row): row is OpenTableCheck =>
      !!row && typeof row.table === 'string' && typeof row.clientOrderId === 'string');
  } catch {
    return [];
  }
}

export function readOpenTableCheck(user: string, table: string): OpenTableCheck | undefined {
  return readOpenTableChecks(user).find((row) => row.table === table);
}

export function rememberOpenTableCheck(user: string, check: OpenTableCheck): void {
  if (!check.table || !check.clientOrderId) return;
  const key = keyFor(user);
  if (!key || typeof sessionStorage === 'undefined') return;
  const next = readOpenTableChecks(user).filter((row) => row.table !== check.table);
  next.push(check);
  sessionStorage.setItem(key, JSON.stringify(next));
}

export function forgetOpenTableCheck(user: string, table?: string): void {
  if (!table) return;
  const key = keyFor(user);
  if (!key || typeof sessionStorage === 'undefined') return;
  const next = readOpenTableChecks(user).filter((row) => row.table !== table);
  if (next.length) sessionStorage.setItem(key, JSON.stringify(next));
  else sessionStorage.removeItem(key);
}
