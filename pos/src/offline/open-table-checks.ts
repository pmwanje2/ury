import { v4 as uuid } from 'uuid';
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
// Per-tab session marker. Logout purges it (USER_SESSION_STORAGE_KEYS), so a
// read issued before the purge, or for another user, cannot write the cache.
const SESSION_PREFIX = 'ury_open_table_checks_session';

export interface OpenTableRead { user: string; nonce: string }

export function beginOpenTableRead(user: string | null | undefined): OpenTableRead | null {
  const key = getUserSessionStorageKey(SESSION_PREFIX, user);
  if (!key || !user || typeof sessionStorage === 'undefined') return null;
  let nonce = sessionStorage.getItem(key);
  if (!nonce) { nonce = uuid(); sessionStorage.setItem(key, nonce); }
  return { user, nonce };
}

export function openTableReadCurrent(read: OpenTableRead | null, user: string | null | undefined): read is OpenTableRead {
  if (!read || read.user !== user) return false;
  const key = getUserSessionStorageKey(SESSION_PREFIX, user);
  return !!key && sessionStorage.getItem(key) === read.nonce;
}

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
