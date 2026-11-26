import { useEffect, useRef, useState } from 'react';
import { call } from '@ury/core';
import { t } from '../i18n';
import { getSessionUser, readPendingBillCopy, storePendingBillCopy } from '../../../packages/core/src/storage';

interface CopySession {
  user: string;
  receipt_copy_session: string;
  csrf_token: string;
}

async function liveSession(): Promise<CopySession> {
  const cashier = getSessionUser();
  const { message } = await call.get<{ message: CopySession }>('ury.www.pos.get_receipt_copy_session');
  if (!message?.receipt_copy_session || !message.csrf_token || !message.user || message.user === 'Guest' ||
      (cashier && message.user !== cashier) || getSessionUser() !== cashier) {
    throw new Error('The cashier session changed. Reload the register before printing.');
  }
  return message;
}

export function usePendingBillCopies() {
  const booted = Boolean((window as Window & { frappe?: { boot?: object } }).frappe?.boot);
  const [transient] = useState(() => new Map<string, string>());
  const [display, setDisplay] = useState<CopySession>();
  const active = useRef<CopySession>(undefined);

  // A remounted page must verify its login before displaying a prior request.
  useEffect(() => {
    if (!booted) return;
    let mounted = true;
    liveSession().then(session => {
      if (mounted) setDisplay(current => current ?? session);
    }).catch(() => undefined);
    return () => { mounted = false; };
  }, [booted]);

  function slot(invoice: string, session?: CopySession) {
    return 'ury_pending_bill_copy:' + JSON.stringify([session?.user ?? getSessionUser(), session?.receipt_copy_session, invoice]);
  }

  function pending(invoice: string) {
    const cashier = getSessionUser();
    if (booted && (!display || (cashier && display.user !== cashier))) return null;
    const name = slot(invoice, display);
    const key = booted ? readPendingBillCopy(name) : transient.get(name);
    return key ? { slot: name, key } : null;
  }

  async function verifySession() {
    // Another tab may have authenticated the same cashier since this page booted.
    const session = await liveSession();
    window.csrf_token = session.csrf_token;
    active.current = session;
    setDisplay(session);
  }

  function start(invoice: string, retry: ReturnType<typeof pending>, approvedKey?: string) {
    const session = active.current;
    const cashier = getSessionUser();
    if (booted && (!session || (cashier && session.user !== cashier))) {
      throw new Error('Verify the cashier session before printing.');
    }
    const name = slot(invoice, session);
    const existing = session ? readPendingBillCopy(name) : transient.get(name);
    // Bind a retry to the request shown when the cashier clicked, before live verification.
    if (retry && (retry.slot !== name || retry.key !== existing)) {
      throw new Error(t('order.copy_retry_changed'));
    }
    if (approvedKey && existing !== approvedKey) throw new Error('Request approval in the current cashier session.');
    if (existing) return existing;
    const key = crypto.randomUUID();
    if (session) storePendingBillCopy(name, key);
    else transient.set(name, key);
    return key;
  }

  function discard(invoice: string, key: string) {
    const session = active.current;
    const name = slot(invoice, session);
    if (session) {
      if (readPendingBillCopy(name) === key) storePendingBillCopy(name, null);
    } else if (transient.get(name) === key) {
      transient.delete(name);
    }
  }

  return { pending, verifySession: booted ? verifySession : undefined, start, discard };
}
