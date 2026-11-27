import { floorFetch } from '@ury/core/floor-request';
import { currencyFractionDigits, resolveDefaultCustomer, type DefaultCustomerConfig } from '@ury/core';
import { v4 as uuid } from 'uuid';
import { t } from '../i18n';
import { isGatewayUnreachableStatus } from './gateway';
import { forgetOpenTableCheck, readOpenTableCheck, rememberOpenTableCheck, type OpenTableCheck } from './open-table-checks';

export function offlineTolerance(currency?: string | null): number {
  const digits = currency ? currencyFractionDigits(currency) : 2;
  return 0.5 * Math.pow(10, -digits);
}

export interface OfflinePaymentModeConfig {
  mode_of_payment: string;
  type?: string;
  allow_offline: boolean;
  requires_reference: boolean;
}

export interface OfflineProfile extends DefaultCustomerConfig {
  name: string;
  currency?: string;
  ury_offline_capture?: unknown;
  role_allowed_for_billing?: Array<{ role: string }>;
  role_restricted_for_table_order?: Array<{ role: string }>;
  custom_offline_peer_relay_url?: string | null;
  custom_offline_collision_policy?: string | null;
  branch?: string | null;
  offline_tender_limit?: number;
  approval_currency?: string;
  approval_conversion_rate?: number | null;
  offline_payment_modes?: OfflinePaymentModeConfig[];
  room_charge_modes?: string[];
  print_type?: string;
  qz_host?: string | null;
  printer?: string | null;
  print_format?: string | null;
  efris_print_wait_seconds?: number;
}

export function offlineCompanyAmount(profile: OfflineProfile, amount: number): number | null {
  const currency = profile.approval_currency || profile.currency;
  const rate = currency?.toUpperCase() === profile.currency?.toUpperCase()
    ? 1
    : Number(profile.approval_conversion_rate);
  if (!Number.isFinite(rate) || rate <= 0) return null;
  const factor = 10 ** (currency ? currencyFractionDigits(currency) : 2);
  return Math.round((amount * rate + 1e-12) * factor) / factor;
}

export function exceedsOfflineTenderLimit(profile: OfflineProfile, amount: number): boolean {
  const limit = Number(profile.offline_tender_limit || 0);
  if (limit <= 0) return false;
  const companyAmount = offlineCompanyAmount(profile, amount);
  return companyAmount === null || companyAmount > limit;
}

export function resolveOfflineModeConfig(
  profile: OfflineProfile | null | undefined,
  modeOfPayment: string,
): OfflinePaymentModeConfig | null {
  if (!profile || !modeOfPayment) return null;
  if (profile.room_charge_modes?.includes(modeOfPayment)) return null;
  if (!Array.isArray(profile.offline_payment_modes)) return null;
  const match = profile.offline_payment_modes.find((row) => row.mode_of_payment === modeOfPayment);
  if (!match || !match.allow_offline || match.type === 'Phone') return null;
  return match;
}
export interface OfflineUser { name: string; roles: string[] }
export interface OfflineRound {
  round_id: string;
  items: Array<{ item: string; item_name: string; qty: number; rate: number; comment?: string; ury_held?: number; reservation_line_key?: string }>;
  captured_at: string;
  expected_total: number;
  offline_printed: number;
}
// Fresh carts can repeat the same dish key. The live attempt and its replay
// share one round ID and line order, even when the acknowledgement is lost.
export function roundItems<T extends { reservation_line_key?: string }>(items: T[], roundId: string) {
  return items.map((item, index) => item.reservation_line_key
    ? { ...item, reservation_line_key: `${roundId}:${index}` } : item);
}
export interface OfflineOrder {
  pos_profile: string; customer?: string; cashier?: string; owner?: string; waiter?: string;
  items: OfflineRound['items']; no_of_pax: number; invoice: string | null; last_invoice: string | null;
  mode_of_payment?: string; order_type: string; table?: string; room?: string; comments?: string | null;
}
export interface OfflineSettlement {
  fiscal_receipt_requested?: 0 | 1;
  kind: 'cash' | 'mobile-money' | 'offline';
  mode_of_payment: string;
  amount: number;
  reference?: string;
  reference_date?: string;
  room_number?: string;
  custom_tender_currency?: string;
  custom_tender_amount?: number;
  needs_online_approval?: boolean;
}
function storedTender(tender: OfflineSettlement): OfflineSettlement {
  return {
    ...tender,
    reference: tender.reference?.trim(),
    fiscal_receipt_requested: tender.fiscal_receipt_requested ?? 1,
  };
}

export interface OfflineEntry {
  sequence?: number; owner: string; profile: string; client_order_id: string; outage_id: string;
  order: OfflineOrder; rounds: OfflineRound[]; settlement?: OfflineSettlement; bill_printed?: boolean;
  state: 'pending' | 'review'; reason?: string; next_round: number;
  phase: 'order' | 'print' | 'settle'; server_invoice?: string;
  needs_approval?: boolean; offline_approval?: string; offline_reason?: string;
}
export type OfflinePrint = (entry: OfflineEntry, kind: 'kot' | 'bill') => boolean | Promise<boolean>;
export type OfflineSettledHook = (settled: { invoice: string; entry: OfflineEntry }) => void | Promise<void>;
interface Options { profile: OfflineProfile; user: OfflineUser; request?: typeof fetch; print?: OfflinePrint; onSettled?: OfflineSettledHook }

const DB = 'ury-pos-offline-capture';
const LOCK = 'ury-pos-offline-capture';
export const OUTBOX_CHANGED = 'ury-outbox-changed';
const changed = () => { if (typeof window !== 'undefined') window.dispatchEvent(new Event(OUTBOX_CHANGED)); };

// Only inserts and removals can change whether an unconfirmed tab may show the POS.
const OUTBOX_MEMBERSHIP = 'ury-outbox-membership';
export function recheckOutboxMembership() {
  window.dispatchEvent(new Event(OUTBOX_MEMBERSHIP));
}

function membershipChannel(): BroadcastChannel | undefined {
  try { return new BroadcastChannel(OUTBOX_MEMBERSHIP); } catch { return undefined; }
}

function membershipChanged() {
  recheckOutboxMembership();
  const channel = membershipChannel();
  try { channel?.postMessage(null); } catch { /* The storage event and polling still apply. */ }
  finally { channel?.close(); }
  // Wake other tabs without storing any order or user data. IndexedDB remains authoritative.
  try { localStorage.setItem(OUTBOX_MEMBERSHIP, uuid()); } catch { /* Recheck on focus. */ }
}

export function onOutboxMembershipChange(check: () => void): () => void {
  const channel = membershipChannel();
  if (channel) channel.onmessage = check;
  const storageChanged = (event: StorageEvent) => {
    if (event.key === OUTBOX_MEMBERSHIP) check();
  };
  window.addEventListener('storage', storageChanged);
  window.addEventListener(OUTBOX_MEMBERSHIP, check);
  return () => {
    channel?.close();
    window.removeEventListener('storage', storageChanged);
    window.removeEventListener(OUTBOX_MEMBERSHIP, check);
  };
}

// Resolve only on transaction COMMIT, not on individual request success.
async function transaction<T>(stores: string[], mode: IDBTransactionMode,
  run: (tx: IDBTransaction, result: (value: T) => void) => void): Promise<T> {
  const database = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('orders', { keyPath: 'sequence', autoIncrement: true });
      request.result.createObjectStore('outages');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return new Promise<T>((resolve, reject) => {
    const tx = database.transaction(stores, mode);
    let value: T;
    tx.oncomplete = () => { database.close(); resolve(value); };
    tx.onabort = () => { database.close(); reject(tx.error || new Error('Offline storage transaction aborted')); };
    run(tx, (result) => { value = result; });
  });
}
const all = () => typeof indexedDB === 'undefined' ? Promise.resolve([] as OfflineEntry[]) :
  transaction<OfflineEntry[]>(['orders'], 'readonly', (tx, result) => {
    const request = tx.objectStore('orders').getAll(); request.onsuccess = () => result(request.result);
  });
const save = async (entry: OfflineEntry) => {
  const inserting = entry.sequence === undefined;
  await transaction(['orders'], 'readwrite', (tx, result) => {
    const request = tx.objectStore('orders').put(entry);
    request.onsuccess = () => { entry.sequence = Number(request.result); result(undefined); };
  });
  if (inserting) membershipChanged();
  changed();
};
const remove = async (entry: OfflineEntry) => {
  await transaction(['orders'], 'readwrite', (tx, result) => { tx.objectStore('orders').delete(entry.sequence!); result(undefined); });
  membershipChanged();
  changed();
};
export async function queuedProfilesFor(user: string): Promise<string[]> {
  const rows = await all();
  return [...new Set(rows.filter((row) => row.owner === user).map((row) => row.profile))];
}

export async function assertOutboxEmpty(): Promise<void> {
  if (typeof indexedDB === 'undefined') return;
  const count = await transaction<number>(['orders'], 'readonly', (tx, result) => {
    const request = tx.objectStore('orders').count();
    request.onsuccess = () => result(request.result);
  });
  if (count) throw new Error('Offline outbox has pending or Needs review items. Reconcile them before logout or till close.');
}
export function hasWebLocks(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.locks?.request === 'function';
}

const exclusive = <T,>(run: () => Promise<T>): Promise<T> => {
  if (hasWebLocks()) return navigator.locks.request(LOCK, run);
  return Promise.reject(new Error('This browser cannot lock the offline outbox across tabs. Offline capture is unavailable.'));
};
// Capture, replay, and identity changes share the same cross-tab lock.
export async function withOutboxLogin(
  user: string | null, login: (owner: string | null) => Promise<void>, allowPasswordAlias = false,
): Promise<void> {
  const guarded = async () => {
    const rows = await all();
    const owner = rows[0]?.owner ?? null;
    if (rows.length) {
      const alias = allowPasswordAlias && user && !user.includes('@');
      if (!owner || rows.some(row => row.owner !== owner) || (user !== owner && !alias)) {
        throw new Error(t('floor_pin.owner_required'));
      }
    }
    await login(owner);
  };
  if (hasWebLocks()) await exclusive(guarded);
  else await guarded();
}

export async function withEmptyOutbox(run: () => Promise<void>): Promise<void> {
  const emptyRun = async () => { await assertOutboxEmpty(); await run(); };
  // The local-only logout still checks for orders that need reconciliation.
  if (hasWebLocks()) await exclusive(emptyRun);
  else await emptyRun();
}
export async function purgeEmptyOutbox(): Promise<void> {
  await assertOutboxEmpty();
  if (typeof indexedDB === 'undefined') return;
  await transaction(['orders', 'outages'], 'readwrite', (tx, result) => {
    tx.objectStore('orders').clear(); tx.objectStore('outages').clear(); result(undefined);
  });
  changed();
}
export function offlineCaptureEnabled(profile?: OfflineProfile | null): boolean {
  return [1, '1', true].includes(profile?.ury_offline_capture as number | string | boolean);
}

export function canCaptureOfflineOrder(profile?: OfflineProfile | null, user?: OfflineUser | null, orderType?: string | null): boolean {
  return offlineCaptureEnabled(profile) && canRecoverOfflineOrder(profile, user, orderType);
}

export function canRecoverOfflineOrder(
  profile?: OfflineProfile | null,
  user?: OfflineUser | null,
  orderType?: string | null,
): boolean {
  if (!profile || !user || !user.name || user.name === 'Guest') return false;
  if (!Array.isArray(user.roles) || user.roles.length === 0) return false;
  if (
    orderType === 'Dine In' &&
    profile.role_restricted_for_table_order?.some((row) => user.roles.includes(row.role))
  ) {
    return false;
  }
  return true;
}

export function canSettleOffline(profile?: OfflineProfile | null, user?: OfflineUser | null): boolean {
  return offlineCaptureEnabled(profile) && canRecoverOfflineTender(profile, user);
}

export function canRecoverOfflineTender(profile?: OfflineProfile | null, user?: OfflineUser | null): boolean {
  return (
    canRecoverOfflineOrder(profile, user) &&
    !!profile?.role_allowed_for_billing?.some((row) => user?.roles?.includes(row.role))
  );
}

/** Backwards-compatible alias for billing-capable offline capture (cashier tills). */
export function canCaptureOffline(profile?: OfflineProfile | null, user?: OfflineUser | null): boolean {
  return canSettleOffline(profile, user);
}
class LinkDown extends Error {}
class NotReady extends Error {}
class Refused extends Error {}
class OrderPermission extends Refused {}
class ApprovalRequired extends Refused {}

// The table's open check until it is settled, including one that has already
// replayed some rounds. It keeps its client id across outages.
export function openTableRow(rows: OfflineEntry[], table: string): OfflineEntry | undefined {
  return rows.find((row) => row.order.table === table && row.state === 'pending' && !row.settlement);
}

export class OfflineOutbox {
  replaying = false;
  reachable = typeof navigator === 'undefined' || navigator.onLine !== false;
  verified = false;
  persistent: boolean | null = null;
  private replayPromise?: Promise<void>;
  private scope: string;
  private request: (url: string, init?: RequestInit) => Promise<Response>;
  private print: OfflinePrint;
  private onSettled?: OfflineSettledHook;
  constructor(private options: Options) {
    this.scope = JSON.stringify([options.user.name, options.profile.name]);
    this.request = options.request || ((url, init) => floorFetch(url, init, options.user.name));
    this.print = options.print || (() => false);
    this.onSettled = options.onSettled;
  }
  private get sessionBlockKey() { return `ury_offline_session_blocked:${this.options.user.name}`; }
  get sessionBlocked() { return sessionStorage.getItem(this.sessionBlockKey) !== null; }
  assertSessionAllowed() {
    if (this.sessionBlocked) throw Object.assign(new Error('Verify the cashier session online before taking orders or payments.'), {
      code: 'URY_OFFLINE_SESSION_UNVERIFIED' as const,
    });
  }
  private replayFailure(error: unknown) {
    this.reachable = !(error instanceof LinkDown);
    // A later outage cannot undo a server denial, even after the store is reloaded.
    if (this.reachable) sessionStorage.setItem(this.sessionBlockKey, '1');
  }
  private allowed(orderType?: string | null) {
    this.assertSessionAllowed();
    if (!canCaptureOfflineOrder(this.options.profile, this.options.user, orderType)) {
      throw new Error('Offline order capture requires an opted-in POS Profile and permitted staff role.');
    }
    if (this.replaying) throw new Error('The outbox is Replaying. Wait before capturing or settling.');
  }
  private allowedSettle() {
    this.assertSessionAllowed();
    if (!canSettleOffline(this.options.profile, this.options.user)) {
      throw new Error('Offline settlement requires an opted-in POS Profile and its billing role.');
    }
    if (this.replaying) throw new Error('The outbox is Replaying. Wait before capturing or settling.');
  }
  list(): Promise<OfflineEntry[]> {
    return all().then((rows) => rows.filter((row) => row.owner === this.options.user.name && row.profile === this.options.profile.name));
  }
  assertEmpty = assertOutboxEmpty;
  async purge(): Promise<void> { await withEmptyOutbox(purgeEmptyOutbox); }
  private async tryPrint(entry: OfflineEntry, kind: 'kot' | 'bill'): Promise<boolean> {
    try { return await this.print(entry, kind); } catch { return false; }
  }
  async reprint(clientId: string, kind: 'kot' | 'bill'): Promise<void> {
    await exclusive(async () => {
      this.assertSessionAllowed();
      const entry = (await this.list()).find((row) => row.client_order_id === clientId);
      if (!entry || entry.state !== 'pending') throw new Error('Only pending offline orders can be reprinted.');
      if (!canRecoverOfflineOrder(this.options.profile, this.options.user, entry.order.order_type) ||
          (kind === 'bill' && !canRecoverOfflineTender(this.options.profile, this.options.user))) {
        throw new Error(t('offline.retry_unavailable'));
      }
      if (kind === 'bill') {
        if (!entry.settlement) throw new Error('Settle the provisional bill first.');
        entry.bill_printed = await this.tryPrint(entry, kind);
      } else {
        for (let index = 0; index < entry.rounds.length; index++) {
          const round = entry.rounds[index];
          if (round.offline_printed) continue;
          const printEntry = { ...entry, rounds: entry.rounds.slice(0, index + 1) };
          round.offline_printed = await this.tryPrint(printEntry, kind) ? 1 : 0;
          await save(entry);
          if (!round.offline_printed) break;
        }
      }
      await save(entry);
    });
  }
  private async rememberPersistence(): Promise<void> {
    if (this.persistent !== null) return;
    try { this.persistent = await navigator.storage?.persist?.() || false; } catch { this.persistent = false; }
  }
  private async storeRound(entry: OfflineEntry, round: OfflineRound): Promise<OfflineEntry> {
    entry.rounds.push(round);
    await save(entry);
    if (await this.tryPrint(entry, 'kot')) { round.offline_printed = 1; await save(entry); }
    return entry;
  }
  private round(items: OfflineRound['items'], expected: number, roundId = uuid()): OfflineRound {
    return { items: roundItems(structuredClone(items), roundId), round_id: roundId, captured_at: new Date().toISOString(), expected_total: expected, offline_printed: 0 };
  }
  // One open check per table, same as the server. A caller-supplied client id
  // keeps the total it was given (already cumulative). Auto-bind adds the new
  // items onto the open check and does not mint another id.
  private async appendToOpenTable(order: OfflineOrder, total: number, rows: OfflineEntry[], outage: string, roundId?: string): Promise<OfflineEntry | undefined> {
    if (!order.table) return undefined;
    const local = openTableRow(rows, order.table);
    if (local) {
      await this.rememberPersistence();
      const last = local.rounds[local.rounds.length - 1]?.expected_total ?? 0;
      return this.storeRound(local, this.round(order.items, last + total, roundId));
    }
    const known = readOpenTableCheck(this.options.user.name, order.table);
    if (!known || rows.some((row) => row.client_order_id === known.clientOrderId)) return undefined;
    await this.rememberPersistence();
    const adopted = structuredClone(order);
    if (known.customer !== undefined) adopted.customer = known.customer;
    if (known.orderType !== undefined) adopted.order_type = known.orderType;
    if (known.noOfPax !== undefined) adopted.no_of_pax = known.noOfPax;
    if (known.room !== undefined) adopted.room = known.room;
    adopted.table = known.table;
    const entry: OfflineEntry = {
      owner: this.options.user.name, profile: this.options.profile.name, client_order_id: known.clientOrderId,
      outage_id: outage, order: adopted, rounds: [], state: 'pending', next_round: 0, phase: 'order',
      ...(known.invoice ? { server_invoice: known.invoice } : {}),
    };
    return this.storeRound(entry, this.round(order.items, (known.grandTotal ?? 0) + total, roundId));
  }
  async capture(order: OfflineOrder, total: number, clientId?: string,
    reserved?: { clientOrderId: string; roundId: string }): Promise<OfflineEntry> {
    this.allowed(order.order_type);
    if (order.items.some(item => item.ury_held)) throw Object.assign(new Error(t('hold_fire.offline')), { code: 'HOLD_OFFLINE' });
    if (order.invoice || order.last_invoice) throw new Error('Rounds on online orders are refused offline.');
    if (order.pos_profile !== this.options.profile.name) throw new Error('Offline order belongs to another profile.');
    if (!order.items.length || order.items.some((item) => !Number.isFinite(item.qty) || item.qty <= 0)) throw new Error('Offline rounds require positive quantities; reductions are refused.');
    if (!Number.isFinite(total) || total < 0) throw new Error('Offline total must be a finite nonnegative amount.');
    return exclusive(async () => {
      this.assertSessionAllowed();
      const outage = await transaction<string>(['outages'], 'readwrite', (tx, result) => {
        const store = tx.objectStore('outages'); const request = store.get(this.scope);
        request.onsuccess = () => { const id = request.result || uuid(); store.put(id, this.scope); result(id); };
      });
      const rows = await this.list();
      // A round already queued under the reserved round id is the same round.
      if (!clientId && reserved?.roundId) {
        const same = rows.find((row) => row.rounds.some((round) => round.round_id === reserved.roundId));
        if (same) return same;
      }
      if (!clientId) {
        const appended = await this.appendToOpenTable(order, total, rows, outage, reserved?.roundId);
        if (appended) return appended;
      }
      const previous = clientId ? rows.find((row) => row.client_order_id === clientId) : undefined;
      if (clientId && (!previous || previous.outage_id !== outage || previous.state !== 'pending' || previous.next_round)) throw new Error('Extra rounds require an order from this same outage.');
      if (previous && (['customer', 'table', 'room', 'order_type', 'no_of_pax'] as const).some((key) => previous.order[key] !== order[key])) throw new Error('Extra rounds cannot change the order context, customer or table offline.');
      if (reserved && !previous && !order.customer && (order.order_type === 'Dine In' || order.order_type === 'Take Away')) {
        const customer = resolveDefaultCustomer(order.order_type, this.options.profile)?.id;
        if (!customer) throw new Error(t('errors.select_customer'));
        order = { ...order, customer };
      }
      if (previous?.settlement) throw new Error('This order already has a settlement.');
      await this.rememberPersistence();
      const round = this.round(order.items, total, !previous ? reserved?.roundId : undefined);
      const entry: OfflineEntry = previous || { owner: this.options.user.name, profile: this.options.profile.name,
        client_order_id: reserved?.clientOrderId || uuid(), outage_id: outage, order: structuredClone(order), rounds: [], state: 'pending', next_round: 0, phase: 'order' };
      return this.storeRound(entry, round);
    });
  }
  private validateSettlementTender(tender: OfflineSettlement): void {
    if (!['cash', 'mobile-money', 'offline'].includes(tender.kind)) {
      throw new Error('Offline settlement accepts configured offline payment methods only.');
    }
    if (Array.isArray(this.options.profile.offline_payment_modes)) {
      const cfg = resolveOfflineModeConfig(this.options.profile, tender.mode_of_payment);
      if (!cfg) {
        throw new Error(t('offline.tender_not_allowed', { mode: tender.mode_of_payment }));
      }
      if (cfg.requires_reference && !tender.reference?.trim()) {
        throw new Error(t('offline.reference_required', { mode: tender.mode_of_payment }));
      }
    } else if (tender.kind === 'mobile-money' && !tender.reference?.trim()) {
      throw new Error('Type the manual mobile-money reference.');
    }
    const limit = Number(this.options.profile.offline_tender_limit || 0);
    if (exceedsOfflineTenderLimit(this.options.profile, tender.amount) && !tender.needs_online_approval) {
      throw new Error(t('offline.over_limit_error', { limit }));
    }
  }
  async settle(clientId: string, tender: OfflineSettlement): Promise<OfflineEntry> {
    this.allowedSettle();
    this.validateSettlementTender(tender);
    return exclusive(async () => {
      this.assertSessionAllowed();
      const entry = (await this.list()).find((row) => row.client_order_id === clientId);
      if (!entry || entry.state !== 'pending' || entry.next_round) throw new Error('Only an offline order awaiting replay can be settled here.');
      if (entry.settlement) throw new Error('This order already has a settlement.');
      if (!Number.isFinite(tender.amount) || tender.amount < entry.rounds[entry.rounds.length - 1].expected_total) throw new Error('Tender must cover the provisional bill total.');
      entry.settlement = storedTender(tender);
      forgetOpenTableCheck(this.options.user.name, entry.order.table);
      if (tender.reference) entry.order.comments = [entry.order.comments, `Offline payment reference (${tender.mode_of_payment}): ${tender.reference.trim()}`].filter(Boolean).join('\n');
      await save(entry);
      entry.bill_printed = await this.tryPrint(entry, 'bill'); await save(entry);
      return entry;
    });
  }

  async queueGatewaySettlement(input: {
    clientOrderId: string; roundId: string; invoice: string; customer?: string; table?: string | null;
    cashier?: string; owner?: string; orderType?: string; settlement: OfflineSettlement;
  }): Promise<OfflineEntry> {
    this.allowedSettle();
    this.validateSettlementTender(input.settlement);
    if (!Number.isFinite(input.settlement.amount) || input.settlement.amount < 0) throw new Error('Offline total must be a finite nonnegative amount.');
    return exclusive(async () => {
      this.assertSessionAllowed();
      const existing = (await this.list()).find((row) => row.client_order_id === input.clientOrderId);
      if (existing) {
        if (!existing.settlement) {
          existing.settlement = storedTender(input.settlement);
          existing.bill_printed = true;
          if (input.invoice) existing.server_invoice = existing.server_invoice || input.invoice;
          await save(existing);
        }
        return existing;
      }
      const outage = await transaction<string>(['outages'], 'readwrite', (tx, result) => {
        const store = tx.objectStore('outages'); const request = store.get(this.scope);
        request.onsuccess = () => { const id = request.result || uuid(); store.put(id, this.scope); result(id); };
      });
      const entry: OfflineEntry = {
        owner: this.options.user.name, profile: this.options.profile.name, client_order_id: input.clientOrderId,
        outage_id: outage, state: 'pending', next_round: 1, phase: 'settle', server_invoice: input.invoice, bill_printed: true,
        order: { pos_profile: this.options.profile.name, customer: input.customer, cashier: input.cashier, owner: input.owner,
          waiter: this.options.user.name, items: [], no_of_pax: 1, invoice: null, last_invoice: null,
          order_type: input.orderType || 'Take Away', table: input.table || undefined },
        rounds: [{ round_id: input.roundId, items: [], captured_at: new Date().toISOString(), expected_total: input.settlement.amount, offline_printed: 1 }],
        settlement: storedTender(input.settlement),
      };
      await save(entry);
      return entry;
    });
  }
  private async call(method: string, payload?: Record<string, unknown>): Promise<unknown> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    let response: Response;
    try { response = await this.request(`/api/method/${method}`, { method: payload ? 'POST' : 'GET', signal: controller.signal,
      credentials: 'same-origin', redirect: 'manual', cache: 'no-store',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json',
        'X-Frappe-CSRF-Token': (window as Window & { csrf_token?: string }).csrf_token || '' },
      body: payload ? JSON.stringify(payload) : undefined });
    } catch (error) {
      if (error instanceof TypeError && !controller.signal.aborted) throw new LinkDown('Network unavailable');
      throw error;
    } finally { clearTimeout(timeout); }
    if (isGatewayUnreachableStatus(response.status)) throw new LinkDown('Gateway unavailable');
    if (response.redirected || response.status === 0 || response.status >= 300 && response.status < 400 || /\/login|cloudflareaccess/i.test(response.url) || !response.headers.get('Content-Type')?.includes('application/json')) throw new NotReady('Server session is not ready');
    if (response.status >= 500 || response.status === 429) throw new NotReady('Server is not yet available');
    let body: { message?: unknown; exc_type?: string; exception?: string; _server_messages?: string };
    try { body = await response.json(); } catch { throw new NotReady('Server did not return JSON'); }
    const detailMsg = body._server_messages || body.exception || '';
    if (
      payload &&
      (detailMsg.includes('POS-AUTH-REQUIRED') || detailMsg.includes('POS-OFFLINE-OVER-LIMIT'))
    ) {
      throw new ApprovalRequired(detailMsg || body.exc_type || 'POS-AUTH-REQUIRED');
    }
    if (payload && response.status === 403 && body.exc_type === 'PermissionError') throw new OrderPermission(body.exc_type);
    if (response.status === 401 || response.status === 403 || body.exc_type === 'CSRFTokenError') throw new NotReady('Server session is not ready');
    if (!response.ok || body.exc_type || body.exception) throw new Refused(body._server_messages || body.exception || body.exc_type || `HTTP ${response.status}`);
    if (typeof body.message === 'object' && body.message && 'status' in body.message && body.message.status === 'Failure') throw new Refused('Server refused the order');
    return body.message;
  }
  private rememberOpen(entry: OfflineEntry): void {
    if (!entry.order.table) return;
    const check: OpenTableCheck = {
      table: entry.order.table, clientOrderId: entry.client_order_id, invoice: entry.server_invoice,
      grandTotal: entry.rounds[entry.rounds.length - 1]?.expected_total, customer: entry.order.customer,
      orderType: entry.order.order_type, noOfPax: entry.order.no_of_pax, room: entry.order.room,
    };
    rememberOpenTableCheck(entry.owner, check);
  }
  private async verifyCashier(): Promise<void> {
    const user = await this.call('frappe.auth.get_logged_user');
    if (user !== this.options.user.name) throw new NotReady('Cashier session does not match');
    sessionStorage.removeItem(this.sessionBlockKey);
    this.reachable = true;
    this.verified = true;
  }
  async replay(stillAuthorized?: () => boolean): Promise<void> {
    if (!canRecoverOfflineOrder(this.options.profile, this.options.user)) return;
    if (this.replayPromise) return this.replayPromise;
    this.replaying = true; changed();
    this.replayPromise = exclusive(async () => {
      try {
        if (stillAuthorized && !stillAuthorized()) return;
        if (!offlineCaptureEnabled(this.options.profile) && !(await this.list()).length) return;
        await this.verifyCashier();
        for (const entry of await this.list()) {
          if (entry.state !== 'pending') continue;
          if (!canRecoverOfflineOrder(this.options.profile, this.options.user, entry.order.order_type)) continue;
          if (entry.rounds.some((round) => !round.offline_printed) || entry.settlement && !entry.bill_printed) break;
          if (entry.settlement && !canRecoverOfflineTender(this.options.profile, this.options.user)) break;
          try {
            while (entry.next_round < entry.rounds.length) {
              const round = entry.rounds[entry.next_round];
              const reply = await this.call('ury.ury.doctype.ury_order.ury_order.sync_order', { ...entry.order,
                items: round.items, invoice: entry.server_invoice || null, last_invoice: entry.server_invoice || null,
                client_order_id: entry.client_order_id, round_id: round.round_id, captured_at: round.captured_at,
                expected_total: round.expected_total, offline_printed: 1, append: 1 });
              if (!reply || typeof reply !== 'object' || !('name' in reply) || typeof reply.name !== 'string') throw new NotReady('Order acknowledgement is incomplete');
              entry.server_invoice = reply.name; entry.next_round++; await save(entry);
            }
            if (!entry.settlement) { this.rememberOpen(entry); await remove(entry); continue; }
            if (!canRecoverOfflineTender(this.options.profile, this.options.user)) break;
            if (entry.phase === 'order') { entry.phase = 'print'; await save(entry); }
            if (entry.phase === 'print') { await this.call('ury.ury.api.ury_print.qz_print_update', { invoice: entry.server_invoice }); entry.phase = 'settle'; await save(entry); }
            const paymentRow: Record<string, unknown> = {
              mode_of_payment: entry.settlement.mode_of_payment,
              amount: entry.settlement.amount,
              ...(entry.settlement.reference ? { reference_no: entry.settlement.reference } : {}),
              ...(entry.settlement.reference_date ? { reference_date: entry.settlement.reference_date } : {}),
              ...(entry.settlement.custom_tender_currency ? { custom_tender_currency: entry.settlement.custom_tender_currency } : {}),
              ...(entry.settlement.custom_tender_amount !== undefined ? { custom_tender_amount: entry.settlement.custom_tender_amount } : {}),
            };
            const reply = await this.call('ury.ury.doctype.ury_order.ury_order.make_invoice', {
              customer: entry.order.customer, cashier: entry.order.cashier, owner: entry.order.owner, table: entry.order.table || null,
              pos_profile: entry.profile, invoice: entry.server_invoice, client_order_id: entry.client_order_id,
              round_id: entry.rounds[entry.rounds.length - 1].round_id,
              payments: [paymentRow],
              offline_replay: 1,
              fiscal_receipt_requested: entry.settlement.fiscal_receipt_requested ?? 1,
              ...(entry.offline_approval ? { offline_approval: entry.offline_approval, offline_reason: entry.offline_reason || '' } : {}),
              ...(entry.settlement.room_number ? { room_number: entry.settlement.room_number } : {}),
            });
            // v16 make_invoice answers {name, status}; a settled retry answers the same.
            if (!reply || typeof reply !== 'object' || !('status' in reply) || reply.status !== 'Submitted') throw new NotReady('Settlement acknowledgement is incomplete');
            const settledInvoice = ('name' in reply && typeof reply.name === 'string' && reply.name) || entry.server_invoice;
            forgetOpenTableCheck(entry.owner, entry.order.table);
            await remove(entry);
            if (this.onSettled && settledInvoice) {
              try {
                await this.onSettled({ invoice: settledInvoice, entry });
              } catch {
                // Settlement already committed; post-replay fiscal print errors stay non-blocking.
              }
            }
          } catch (error) {
            if (!(error instanceof Refused)) { this.replayFailure(error); break; }
            if (error instanceof OrderPermission) {
              // Keep the session blocked if the identity probe loses its link.
              sessionStorage.setItem(this.sessionBlockKey, '1');
              await this.verifyCashier();
            }
            entry.state = 'review';
            entry.reason = error.message;
            entry.offline_approval = undefined;
            entry.offline_reason = undefined;
            if (error instanceof ApprovalRequired) {
              entry.needs_approval = true;
            }
            await save(entry);
          }
        }
        if ((await this.list()).length === 0) {
          await transaction(['outages'], 'readwrite', (tx, result) => { tx.objectStore('outages').delete(this.scope); result(undefined); });
        }
      } catch (error) { this.replayFailure(error); }
    }).finally(() => { this.replaying = false; this.replayPromise = undefined; changed(); });
    return this.replayPromise;
  }

  async resolveReviewed(clientId: string, receipt: string): Promise<void> {
    await exclusive(async () => {
      const { profile, user } = this.options;
      await this.verifyCashier();
      const entry = (await this.list()).find((row) => row.client_order_id === clientId);
      if (!entry || entry.state !== 'review' || offlineCaptureEnabled(profile) ||
          !canRecoverOfflineOrder(profile, user, entry.order.order_type) ||
          (entry.settlement && !canRecoverOfflineTender(profile, user))) throw new Error(t('offline.retry_unavailable'));

      const name = receipt.trim();
      const mismatch = () => new Error(t('offline.invoice_mismatch'));
      if (!name || (entry.server_invoice && entry.server_invoice !== name)) throw mismatch();
      const invoice = await this.call('frappe.client.get_value', {
        doctype: 'POS Invoice', filters: { name },
        fieldname: ['name', 'docstatus', 'pos_profile', 'customer', 'grand_total', 'currency', 'is_return', 'ury_client_order_id'],
      }) as Record<string, unknown> | null;
      const total = entry.rounds[entry.rounds.length - 1].expected_total;
      if (!invoice || invoice.name !== name || invoice.docstatus !== 1 || invoice.is_return !== 0 ||
          invoice.pos_profile !== profile.name || (entry.order.customer && invoice.customer !== entry.order.customer) ||
          (profile.currency && invoice.currency !== profile.currency) ||
          (invoice.ury_client_order_id ? invoice.ury_client_order_id !== clientId : entry.server_invoice !== name) ||
          typeof invoice.grand_total !== 'number' || !Number.isFinite(invoice.grand_total) ||
          Math.abs(invoice.grand_total - total) > offlineTolerance(profile.currency)) throw mismatch();
      forgetOpenTableCheck(entry.owner, entry.order.table);
      await remove(entry);
    });
  }

  async retryReviewed(clientId: string): Promise<void> {
    await exclusive(async () => {
      await this.verifyCashier();
      const row = (await this.list()).find((entry) => entry.client_order_id === clientId);
      if (!row || row.state !== 'review' || row.needs_approval) throw new Error(t('offline.retry_unavailable'));
      row.state = 'pending';
      row.reason = undefined;
      await save(row);
    });
    await this.replay();
  }

  async approveAndReplay(clientId: string, token: string, reason: string): Promise<void> {
    if (!token || !token.trim()) throw new Error('Manager approval token is required.');
    await exclusive(async () => {
      const entry = (await this.list()).find((row) => row.client_order_id === clientId);
      if (!entry) throw new Error('Offline order was not found.');
      entry.offline_approval = token.trim();
      entry.offline_reason = (reason || '').trim();
      entry.state = 'pending';
      entry.needs_approval = false;
      entry.reason = undefined;
      await save(entry);
    });
    await this.replay();
  }
}
