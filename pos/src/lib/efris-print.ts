import { call } from '@ury/core';
import { showToast } from '@ury/ui';
import { t } from '../i18n';
import { printBillCopy } from './print';
import type { PosProfileCombined } from './pos-profile-api';

export const DEFAULT_EFRIS_PRINT_WAIT_SECONDS = 5;
export const MAX_EFRIS_PRINT_WAIT_SECONDS = 15;
const DEFAULT_POLL_INTERVAL_MS = 500;
const STORAGE_KEY = 'ury_pending_fiscal_reprints';
const EFRIS_STATUS_METHOD = 'uganda_compliance.efris.efris_queue.get_efris_status';
const QUEUE_REPRINT_METHOD = 'ury.ury_pos.api.queue_fiscal_reprint';

export interface EfrisStatusResponse {
  efris_status?: string | null;
  efris_einvoice_status?: string | null;
  fdn?: string | null;
  e_invoice?: string | null;
}

export interface WaitForEfrisResult {
  outcome: 'fiscalised' | 'timed_out' | 'not_applicable';
  fdn?: string | null;
  status?: string | null;
}

export interface PendingFiscalReprintEntry {
  invoice: string;
  partnerInvoice?: string | null;
  queuedAt: string;
}

export type ReprintPosProfile = Pick<Partial<PosProfileCombined>, 'print_type' | 'qz_host' | 'printer' | 'efris_print_wait_seconds'> & {
  qz_receipt_queue?: string | null;
  custom_qz_receipt_queue?: string | null;
};

export function resolveEfrisPrintWaitSeconds(raw: unknown): number {
  if (raw === undefined || raw === null || raw === '') {
    return DEFAULT_EFRIS_PRINT_WAIT_SECONDS;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    return DEFAULT_EFRIS_PRINT_WAIT_SECONDS;
  }
  return Math.max(0, Math.min(Math.floor(parsed), MAX_EFRIS_PRINT_WAIT_SECONDS));
}

type FetchEfrisOutcome =
  | { kind: 'ok'; data: EfrisStatusResponse | null }
  | { kind: 'not_applicable' }
  | { kind: 'transient_error' };

function isMethodUnavailableError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false;
  const record = err as Record<string, unknown>;
  const status = Number(record.httpStatus ?? record.status ?? record.statusCode ?? 0);
  if (status === 404) return true;
  const text = [record.message, record.exc_type, record.exc, record._server_messages]
    .filter((v): v is string => typeof v === 'string')
    .join(' ');
  return /Failed to get method for command|No module named|AppNotInstalledError|is not whitelisted/i.test(text);
}

async function fetchEfrisStatus(invoice: string): Promise<FetchEfrisOutcome> {
  if (typeof call.get !== 'function') {
    return { kind: 'not_applicable' };
  }
  try {
    const res = await call.get<{ message?: EfrisStatusResponse }>(EFRIS_STATUS_METHOD, {
      doctype: 'POS Invoice',
      name: invoice,
    });
    return { kind: 'ok', data: res?.message ?? null };
  } catch (err) {
    return isMethodUnavailableError(err) ? { kind: 'not_applicable' } : { kind: 'transient_error' };
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function evaluateInvoiceGroup(
  invoice: string,
  partnerInvoice?: string | null,
): Promise<{
  state: 'fiscalised' | 'pending' | 'failed' | 'not_applicable' | 'transient_error';
  fdn?: string | null;
  status?: string | null;
}> {
  const names = partnerInvoice ? [invoice, partnerInvoice] : [invoice];
  const results = await Promise.all(names.map((name) => fetchEfrisStatus(name)));
  if (results.some((r) => r.kind === 'transient_error')) {
    return { state: 'transient_error' };
  }
  const statuses = results.map((r) => (r.kind === 'ok' ? r.data : null));
  const active = statuses.filter((s): s is EfrisStatusResponse => Boolean(s && (s.fdn || s.efris_status)));
  if (active.length === 0) {
    return { state: 'not_applicable' };
  }
  if (active.every((s) => Boolean(s.fdn))) {
    return { state: 'fiscalised', fdn: statuses[0]?.fdn ?? active[0]?.fdn ?? null, status: 'Submitted' };
  }
  if (active.some((s) => s.efris_status === 'Failed')) {
    return { state: 'failed', status: 'Failed' };
  }
  return { state: 'pending', status: active[0]?.efris_status ?? 'Pending' };
}

export async function waitForEfrisFdn({
  invoice,
  partnerInvoice,
  timeoutSeconds,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  invoice: string;
  partnerInvoice?: string | null;
  timeoutSeconds: number;
  pollIntervalMs?: number;
}): Promise<WaitForEfrisResult> {
  const boundMs = Math.max(0, timeoutSeconds * 1000);
  const deadline = Date.now() + boundMs;

  while (true) {
    const current = await evaluateInvoiceGroup(invoice, partnerInvoice);
    if (current.state === 'not_applicable') {
      return { outcome: 'not_applicable' };
    }
    if (current.state === 'fiscalised') {
      return { outcome: 'fiscalised', fdn: current.fdn, status: current.status };
    }
    if (current.state === 'failed') {
      return { outcome: 'timed_out', status: 'Failed' };
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      return { outcome: 'timed_out', status: current.status ?? 'Pending' };
    }
    await sleep(Math.min(pollIntervalMs, remaining));
  }
}

function readPendingQueue(): PendingFiscalReprintEntry[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as PendingFiscalReprintEntry[]) : [];
  } catch {
    return [];
  }
}

function writePendingQueue(entries: PendingFiscalReprintEntry[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    if (entries.length === 0) {
      localStorage.removeItem(STORAGE_KEY);
    } else {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
    }
  } catch {
    // Storage quota or restricted mode; server queue still retains the intent.
  }
}

function deliveryForProfile(posProfile?: ReprintPosProfile | null): 'network' | 'qz' | 'browser' {
  if (posProfile?.print_type === 'network') return 'network';
  if (posProfile?.print_type === 'qz') return 'qz';
  return 'browser';
}

async function registerFiscalCopy(invoice: string, profile?: ReprintPosProfile | null): Promise<boolean> {
  try {
    const response = await call.post(QUEUE_REPRINT_METHOD, { invoice, delivery: deliveryForProfile(profile) });
    return !['DISPATCHED', 'NOT_REQUIRED', 'FAILED', 'CANCELED'].includes(response?.message?.status);
  } catch (error) {
    const refusal = error as { httpStatus?: number; exc_type?: string } | null;
    if (refusal?.httpStatus === 403 || refusal?.exc_type === 'PermissionError') return false;
    throw error;
  }
}

export async function queuePendingFiscalReprint({
  invoice,
  partnerInvoice,
  posProfile,
}: {
  invoice: string;
  partnerInvoice?: string | null;
  posProfile?: ReprintPosProfile | null;
}): Promise<void> {
  const existing = readPendingQueue();
  if (!existing.some((entry) => entry.invoice === invoice)) {
    existing.push({ invoice, partnerInvoice: partnerInvoice ?? null, queuedAt: new Date().toISOString() });
    writePendingQueue(existing);
  }
  if (typeof call.post === 'function') {
    try {
      if (!await registerFiscalCopy(invoice, posProfile)) {
        writePendingQueue(readPendingQueue().filter(entry => entry.invoice !== invoice));
      }
    } catch {
      // Keep local queue entry so the till retries when reachable.
    }
  }
}

function copyProfile(profile: ReprintPosProfile) {
  return {
    print_type: profile.print_type ?? 'socket',
    qz_host: profile.qz_host ?? null,
    printer: profile.printer || profile.qz_receipt_queue || profile.custom_qz_receipt_queue || null,
  };
}

async function dispatchFiscalCopy(invoice: string, profile: ReprintPosProfile) {
  // Local queueing can succeed while the server is offline. Re-registering the
  // same invoice is idempotent and preserves a previously fulfilled fiscal job.
  if (!await registerFiscalCopy(invoice, profile)) return null;
  return printBillCopy({
    orderId: invoice, posProfile: copyProfile(profile), idempotencyKey: `efris-auto-${invoice}`,
  });
}

let flushing = false;

export async function flushPendingFiscalReprints(posProfile?: ReprintPosProfile | null): Promise<void> {
  if (flushing || !posProfile) return;
  const entries = readPendingQueue();
  if (entries.length === 0) return;
  flushing = true;
  try {
    const finished = new Set<string>();
    for (const entry of entries) {
      const current = await evaluateInvoiceGroup(entry.invoice, entry.partnerInvoice);
      if (current.state === 'fiscalised') {
        try {
          const result = await dispatchFiscalCopy(entry.invoice, posProfile);
          finished.add(entry.invoice);
          if (!result) continue;
          showToast?.success?.(
            t('payment.fiscal_reprint_printed', {
              invoice: entry.invoice,
              copy: result.copy_number ?? 1,
              fdn: current.fdn ?? '',
            }),
          );
        } catch {
          // Keep the entry for a later delivery attempt.
        }
      } else if (current.state === 'not_applicable') {
        finished.add(entry.invoice);
      }
    }
    writePendingQueue(readPendingQueue().filter(entry => !finished.has(entry.invoice)));
  } finally {
    flushing = false;
  }
}

export async function triggerPostReplayFiscalPrint({
  invoice,
  partnerInvoice,
  posProfile,
}: {
  invoice: string;
  partnerInvoice?: string | null;
  posProfile?: ReprintPosProfile | null;
}): Promise<void> {
  if (!posProfile) return;
  const waitSeconds = resolveEfrisPrintWaitSeconds(posProfile.efris_print_wait_seconds);
  const outcome = await waitForEfrisFdn({ invoice, partnerInvoice, timeoutSeconds: waitSeconds });
  if (outcome.outcome === 'timed_out') {
    await queuePendingFiscalReprint({ invoice, partnerInvoice, posProfile });
    return;
  }
  if (outcome.outcome === 'not_applicable') {
    await printBillCopy({ orderId: invoice, posProfile: copyProfile(posProfile), idempotencyKey: `offline-copy-${invoice}` });
    return;
  }
  try {
    await dispatchFiscalCopy(invoice, posProfile);
  } catch {
    await queuePendingFiscalReprint({ invoice, partnerInvoice, posProfile });
  }
}
