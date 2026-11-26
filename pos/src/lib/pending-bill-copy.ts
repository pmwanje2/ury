import { useState } from 'react';
import { getSessionUser } from '../../../packages/core/src/storage';

function copySlot(invoice: string) {
  const user = getSessionUser();
  const boot = (window as Window & { frappe?: { boot?: { receipt_copy_session?: string } } }).frappe?.boot;
  const login = boot?.receipt_copy_session;
  const key = 'ury_pending_bill_copy:' + JSON.stringify([user, login, invoice]);
  return { key, persistent: Boolean(login) && user !== 'Guest' };
}

export function usePendingBillCopies() {
  // A register without session boot data can retry for the lifetime of its mounted page.
  const [transient] = useState(() => new Map<string, string>());

  function pending(invoice: string) {
    const slot = copySlot(invoice);
    try {
      return slot.persistent ? sessionStorage.getItem(slot.key) : transient.get(slot.key);
    } catch {
      return null;
    }
  }

  function start(invoice: string, approvedKey?: string) {
    const slot = copySlot(invoice);
    const existing = slot.persistent ? sessionStorage.getItem(slot.key) : transient.get(slot.key);
    if (approvedKey && existing !== approvedKey) throw new Error('Request approval in the current cashier session.');
    if (existing) return existing;
    const key = crypto.randomUUID();
    if (slot.persistent) sessionStorage.setItem(slot.key, key);
    else transient.set(slot.key, key);
    return key;
  }

  function discard(invoice: string, key: string) {
    if (pending(invoice) !== key) return;
    const slot = copySlot(invoice);
    if (slot.persistent) sessionStorage.removeItem(slot.key);
    else transient.delete(slot.key);
  }

  return { pending, start, discard };
}
