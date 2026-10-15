import { v4 as uuid } from 'uuid';
import { forgetOpenTableCheck, readOpenTableCheck, rememberOpenTableCheck, type OpenTableCheck } from './open-table-checks';

export interface OfflineProfile {
  name: string;
  ury_offline_capture?: unknown;
  role_allowed_for_billing?: Array<{ role: string }>;
}
export interface OfflineUser { name: string; roles: string[] }
export interface OfflineRound {
  round_id: string;
  items: Array<{ item: string; item_name: string; qty: number; rate: number; comment?: string }>;
  captured_at: string;
  expected_total: number;
  offline_printed: number;
}
export interface OfflineOrder {
  pos_profile: string; customer?: string; cashier?: string; owner?: string; waiter?: string;
  items: OfflineRound['items']; no_of_pax: number; invoice: string | null; last_invoice: string | null;
  mode_of_payment?: string; order_type: string; table?: string; room?: string; comments?: string | null;
}
export interface OfflineSettlement {
  kind: 'cash' | 'mobile-money'; mode_of_payment: string; amount: number; reference?: string;
}
export interface OfflineEntry {
  sequence?: number; owner: string; profile: string; client_order_id: string; outage_id: string;
  order: OfflineOrder; rounds: OfflineRound[]; settlement?: OfflineSettlement; bill_printed?: boolean;
  state: 'pending' | 'review'; reason?: string; next_round: number;
  phase: 'order' | 'print' | 'settle'; server_invoice?: string;
}
export type OfflinePrint = (entry: OfflineEntry, kind: 'kot' | 'bill') => boolean | Promise<boolean>;
interface Options { profile: OfflineProfile; user: OfflineUser; request?: typeof fetch; print?: OfflinePrint }

const DB = 'ury-pos-offline-capture';
const LOCK = 'ury-pos-offline-capture';
export const OUTBOX_CHANGED = 'ury-outbox-changed';
const changed = () => { if (typeof window !== 'undefined') window.dispatchEvent(new Event(OUTBOX_CHANGED)); };

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
  await transaction(['orders'], 'readwrite', (tx, result) => {
    const request = tx.objectStore('orders').put(entry);
    request.onsuccess = () => { entry.sequence = Number(request.result); result(undefined); };
  }); changed();
};
const remove = async (entry: OfflineEntry) => {
  await transaction(['orders'], 'readwrite', (tx, result) => { tx.objectStore('orders').delete(entry.sequence!); result(undefined); }); changed();
};
export async function assertOutboxEmpty(): Promise<void> {
  if ((await all()).length) throw new Error('Offline outbox has pending or Needs review items. Reconcile them before logout or till close.');
}
const exclusive = <T,>(run: () => Promise<T>): Promise<T> => {
  if (typeof navigator !== 'undefined' && navigator.locks) return navigator.locks.request(LOCK, run);
  return Promise.reject(new Error('This browser cannot lock the offline outbox across tabs. Offline capture is unavailable.'));
};
export async function withEmptyOutbox(run: () => Promise<void>): Promise<void> {
  const emptyRun = async () => { await assertOutboxEmpty(); await run(); };
  // Capture/replay fail closed without Web Locks, so an empty browser can still log out.
  if (typeof navigator !== 'undefined' && navigator.locks) await exclusive(emptyRun);
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
export function canCaptureOffline(profile?: OfflineProfile | null, user?: OfflineUser | null): boolean {
  return !!profile && !!user && user.name !== 'Guest' &&
    [1, '1', true].includes(profile.ury_offline_capture as number | string | boolean) &&
    !!profile.role_allowed_for_billing?.some((row) => user.roles.includes(row.role));
}
class NotReady extends Error {}
class Refused extends Error {}

// The table's open check until it is settled, including one that has already
// replayed some rounds. It keeps its client id across outages.
export function openTableRow(rows: OfflineEntry[], table: string): OfflineEntry | undefined {
  return rows.find((row) => row.order.table === table && row.state === 'pending' && !row.settlement);
}

export class OfflineOutbox {
  replaying = false;
  reachable = typeof navigator === 'undefined' || navigator.onLine !== false;
  persistent: boolean | null = null;
  private replayPromise?: Promise<void>;
  private scope: string;
  private request: typeof fetch;
  private print: OfflinePrint;
  constructor(private options: Options) {
    this.scope = JSON.stringify([options.user.name, options.profile.name]);
    this.request = options.request || ((url, init) => fetch(url, init));
    this.print = options.print || (() => false);
  }
  private allowed() {
    if (!canCaptureOffline(this.options.profile, this.options.user)) throw new Error('Offline capture requires an opted-in POS Profile and its billing role.');
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
    this.allowed();
    await exclusive(async () => {
      const entry = (await this.list()).find((row) => row.client_order_id === clientId);
      if (!entry || entry.state !== 'pending') throw new Error('Only pending offline orders can be reprinted.');
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
  private round(items: OfflineRound['items'], expected: number, roundId?: string): OfflineRound {
    return { items: structuredClone(items), round_id: roundId || uuid(), captured_at: new Date().toISOString(), expected_total: expected, offline_printed: 0 };
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
    this.allowed();
    if (order.invoice || order.last_invoice) throw new Error('Rounds on online orders are refused offline.');
    if (order.pos_profile !== this.options.profile.name) throw new Error('Offline order belongs to another profile.');
    if (!order.items.length || order.items.some((item) => !Number.isFinite(item.qty) || item.qty <= 0)) throw new Error('Offline rounds require positive quantities; reductions are refused.');
    if (!Number.isFinite(total) || total < 0) throw new Error('Offline total must be a finite nonnegative amount.');
    return exclusive(async () => {
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
      if (previous?.settlement) throw new Error('This order already has a settlement.');
      await this.rememberPersistence();
      const round = this.round(order.items, total, !previous ? reserved?.roundId : undefined);
      const entry: OfflineEntry = previous || { owner: this.options.user.name, profile: this.options.profile.name,
        client_order_id: reserved?.clientOrderId || uuid(), outage_id: outage, order: structuredClone(order), rounds: [], state: 'pending', next_round: 0, phase: 'order' };
      return this.storeRound(entry, round);
    });
  }
  async settle(clientId: string, tender: OfflineSettlement): Promise<OfflineEntry> {
    this.allowed();
    if (!['cash', 'mobile-money'].includes(tender.kind)) throw new Error('Offline settlement accepts cash or manual mobile-money only.');
    if (tender.kind === 'mobile-money' && !tender.reference?.trim()) throw new Error('Type the manual mobile-money reference.');
    return exclusive(async () => {
      const entry = (await this.list()).find((row) => row.client_order_id === clientId);
      if (!entry || entry.state !== 'pending' || entry.next_round) throw new Error('Only an offline order awaiting replay can be settled here.');
      if (entry.settlement) throw new Error('This order already has a settlement.');
      if (!Number.isFinite(tender.amount) || tender.amount < entry.rounds[entry.rounds.length - 1].expected_total) throw new Error('Tender must cover the provisional bill total.');
      entry.settlement = { ...tender, reference: tender.reference?.trim() };
      forgetOpenTableCheck(this.options.user.name, entry.order.table);
      if (tender.reference) entry.order.comments = [entry.order.comments, `Manual mobile-money reference: ${tender.reference.trim()}`].filter(Boolean).join('\n');
      await save(entry);
      entry.bill_printed = await this.tryPrint(entry, 'bill'); await save(entry);
      return entry;
    });
  }

  async queueGatewaySettlement(input: {
    clientOrderId: string; roundId: string; invoice: string; customer?: string; table?: string | null;
    cashier?: string; owner?: string; orderType?: string; settlement: OfflineSettlement;
  }): Promise<OfflineEntry> {
    this.allowed();
    if (!['cash', 'mobile-money'].includes(input.settlement.kind)) throw new Error('Offline settlement accepts cash or manual mobile-money only.');
    if (input.settlement.kind === 'mobile-money' && !input.settlement.reference?.trim()) throw new Error('Type the manual mobile-money reference.');
    if (!Number.isFinite(input.settlement.amount) || input.settlement.amount < 0) throw new Error('Offline total must be a finite nonnegative amount.');
    return exclusive(async () => {
      const existing = (await this.list()).find((row) => row.client_order_id === input.clientOrderId);
      if (existing) {
        if (!existing.settlement) {
          existing.settlement = { ...input.settlement, reference: input.settlement.reference?.trim() };
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
        settlement: { ...input.settlement, reference: input.settlement.reference?.trim() },
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
    } finally { clearTimeout(timeout); }
    if (response.redirected || response.status === 0 || response.status >= 300 && response.status < 400 || /\/login|cloudflareaccess/i.test(response.url) || !response.headers.get('Content-Type')?.includes('application/json')) throw new NotReady('Server session is not ready');
    if (response.status >= 500 || response.status === 429) throw new NotReady('Server is not yet available');
    let body: { message?: unknown; exc_type?: string; exception?: string; _server_messages?: string };
    try { body = await response.json(); } catch { throw new NotReady('Server did not return JSON'); }
    if (response.status === 401 || body.exc_type === 'CSRFTokenError' ||
      response.status === 403 && /login to access/i.test([body.exception, body._server_messages].join(' '))) throw new NotReady('Server session is not ready');
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
  async replay(): Promise<void> {
    if (!canCaptureOffline(this.options.profile, this.options.user)) return;
    if (this.replayPromise) return this.replayPromise;
    this.replaying = true; changed();
    this.replayPromise = exclusive(async () => {
      try {
        const user = await this.call('frappe.auth.get_logged_user');
        if (user !== this.options.user.name) { this.reachable = false; return; }
        this.reachable = true;
        for (const entry of await this.list()) {
          if (entry.state !== 'pending') continue;
          if (entry.rounds.some((round) => !round.offline_printed) || entry.settlement && !entry.bill_printed) break;
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
            if (entry.phase === 'order') { entry.phase = 'print'; await save(entry); }
            if (entry.phase === 'print') { await this.call('ury.ury.api.ury_print.qz_print_update', { invoice: entry.server_invoice }); entry.phase = 'settle'; await save(entry); }
            const reply = await this.call('ury.ury.doctype.ury_order.ury_order.make_invoice', {
              customer: entry.order.customer, cashier: entry.order.cashier, owner: entry.order.owner, table: entry.order.table || null,
              pos_profile: entry.profile, invoice: entry.server_invoice, client_order_id: entry.client_order_id,
              round_id: entry.rounds[entry.rounds.length - 1].round_id, payments: [{ mode_of_payment: entry.settlement.mode_of_payment, amount: entry.settlement.amount }], additionalDiscount: null });
            if (!reply || typeof reply !== 'object' || !('docstatus' in reply) || reply.docstatus !== 1) throw new NotReady('Settlement acknowledgement is incomplete');
            forgetOpenTableCheck(entry.owner, entry.order.table);
            await remove(entry);
          } catch (error) {
            if (!(error instanceof Refused)) { this.reachable = false; break; }
            entry.state = 'review'; entry.reason = error.message; await save(entry);
          }
        }
        if ((await this.list()).length === 0) {
          await transaction(['outages'], 'readwrite', (tx, result) => { tx.objectStore('outages').delete(this.scope); result(undefined); });
        }
      } catch { this.reachable = false; }
    }).finally(() => { this.replaying = false; this.replayPromise = undefined; changed(); });
    return this.replayPromise;
  }
}
