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

export type ReprintPosProfile = Pick<Partial<PosProfileCombined>, 'print_type' | 'qz_host' | 'efris_print_wait_seconds'>;

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
      await call.post(QUEUE_REPRINT_METHOD, {
        invoice,
        delivery: deliveryForProfile(posProfile),
      });
    } catch {
      // Keep local queue entry so the till retries when reachable.
    }
  }
}

let flushing = false;

export async function flushPendingFiscalReprints(posProfile?: ReprintPosProfile | null): Promise<void> {
  if (flushing || !posProfile) return;
  const entries = readPendingQueue();
  if (entries.length === 0) return;
  flushing = true;
  try {
    const remaining: PendingFiscalReprintEntry[] = [];
    for (const entry of entries) {
      const current = await evaluateInvoiceGroup(entry.invoice, entry.partnerInvoice);
      if (current.state === 'fiscalised') {
        try {
          const result = await printBillCopy({
            orderId: entry.invoice,
            posProfile: { print_type: posProfile.print_type ?? 'socket', qz_host: posProfile.qz_host ?? null },
            idempotencyKey: `efris-auto-${entry.invoice}`,
          });
          showToast?.success?.(
            t('payment.fiscal_reprint_printed', {
              invoice: entry.invoice,
              copy: result.copy_number ?? 1,
              fdn: current.fdn ?? '',
            }),
          );
        } catch {
          remaining.push(entry);
        }
      } else if (current.state === 'not_applicable') {
        // Non-EFRIS invoice no longer waits for an FDN.
      } else {
        remaining.push(entry);
      }
    }
    writePendingQueue(remaining);
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
  try {
    await printBillCopy({
      orderId: invoice,
      posProfile: { print_type: posProfile.print_type ?? 'socket', qz_host: posProfile.qz_host ?? null },
      idempotencyKey: `efris-auto-${invoice}`,
    });
  } catch {
    await queuePendingFiscalReprint({ invoice, partnerInvoice, posProfile });
  }
}
