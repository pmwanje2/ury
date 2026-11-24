import 'fake-indexeddb/auto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import en from '../src/i18n/locales/en.json';
import fr from '../src/i18n/locales/fr.json';
import es from '../src/i18n/locales/es.json';
import ar from '../src/i18n/locales/ar.json';

type Tree = { [key: string]: string | Tree };

function lookup(tree: Tree, key: string): string | undefined {
  let node: string | Tree | undefined = tree;
  for (const part of key.split('.')) node = typeof node === 'object' ? node[part] : undefined;
  return typeof node === 'string' ? node : undefined;
}

const placeholders = (value: string) => (value.match(/\{\{\w+\}\}/g) ?? []).sort().join(',');
const VENDOR_RE = /\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz)\b/i;

describe('POS-PR-6: G46 Waiter offline capabilities, outbox reference_no passthrough, peer-sync, and read-cache', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
    localStorage.clear();
    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: { request: (_name: string, fn: () => Promise<unknown>) => fn() },
    });
  });

  afterEach(async () => {
    vi.unstubAllGlobals();
    await new Promise<void>((resolve) => {
      const req = indexedDB.deleteDatabase('ury-pos-offline-capture');
      req.onsuccess = req.onerror = req.onblocked = () => resolve();
    });
  });

  it('splits canCaptureOfflineOrder (waiter allowed, restricted blocked) vs canSettleOffline (billing only)', async () => {
    const outboxMod = await import('../src/offline/outbox');
    const canCaptureOfflineOrder = (outboxMod as any).canCaptureOfflineOrder;
    const canSettleOffline = (outboxMod as any).canSettleOffline;
    expect(canCaptureOfflineOrder).toBeTypeOf('function');
    expect(canSettleOffline).toBeTypeOf('function');

    const profile = {
      name: 'Main Till',
      ury_offline_capture: 1,
      role_allowed_for_billing: [{ role: 'Cashier Role' }],
      role_restricted_for_table_order: [{ role: 'Counter Only' }],
    };
    const waiter = { name: 'waiter@hotel.test', roles: ['Floor Server'] };
    const cashier = { name: 'cashier@hotel.test', roles: ['Cashier Role'] };
    const restricted = { name: 'counter@hotel.test', roles: ['Counter Only'] };

    expect(canCaptureOfflineOrder(profile, waiter, 'Dine In')).toBe(true);
    expect(canSettleOffline(profile, waiter)).toBe(false);

    expect(canCaptureOfflineOrder(profile, cashier, 'Dine In')).toBe(true);
    expect(canSettleOffline(profile, cashier)).toBe(true);

    expect(canCaptureOfflineOrder(profile, restricted, 'Dine In')).toBe(false);
    expect(canCaptureOfflineOrder(profile, restricted, 'Take Away')).toBe(true);

    expect(canCaptureOfflineOrder({ ...profile, ury_offline_capture: 0 }, waiter, 'Dine In')).toBe(false);
    expect(canCaptureOfflineOrder(profile, { name: 'Guest', roles: ['Floor Server'] }, 'Dine In')).toBe(false);
  });

  it('allows a waiter to capture an offline order, refuses waiter offline settlement, and forwards reference_no on cashier replay', async () => {
    const { OfflineOutbox } = await import('../src/offline/outbox');
    const profile = {
      name: 'Main Till',
      ury_offline_capture: 1,
      role_allowed_for_billing: [{ role: 'Cashier Role' }],
    };
    const waiter = { name: 'waiter@hotel.test', roles: ['Floor Server'] };
    const cashier = { name: 'cashier@hotel.test', roles: ['Cashier Role'] };
    const order = {
      pos_profile: 'Main Till',
      customer: 'Walk-in Guest',
      items: [{ item: 'TILAPIA', item_name: 'Grilled Tilapia', qty: 1, rate: 25000 }],
      no_of_pax: 2,
      invoice: null,
      last_invoice: null,
      order_type: 'Dine In',
      table: 'T-05',
      waiter: waiter.name,
    };

    const waiterOutbox = new OfflineOutbox({ profile, user: waiter, print: () => true });
    const captured = await waiterOutbox.capture(order, 25000);
    expect(captured.order.table).toBe('T-05');
    await expect(
      waiterOutbox.settle(captured.client_order_id, { kind: 'cash', mode_of_payment: 'Cash', amount: 25000 }),
    ).rejects.toThrow(/billing|settle|cashier/i);

    const sent: Array<{ url: string; body: any }> = [];
    const req = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      sent.push({ url, body });
      if (url.includes('get_logged_user')) {
        return new Response(JSON.stringify({ message: cashier.name }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.includes('sync_order')) {
        return new Response(JSON.stringify({ message: { name: 'POS-INV-9001' } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.includes('make_invoice')) {
        return new Response(JSON.stringify({ message: { name: 'POS-INV-9001', status: 'Submitted' } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify({ message: 'ok' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });

    const cashierOutbox = new OfflineOutbox({ profile, user: cashier, request: req as any, print: () => true });
    const cRow = await cashierOutbox.capture({ ...order, table: undefined, order_type: 'Take Away', waiter: cashier.name }, 25000);
    await cashierOutbox.settle(cRow.client_order_id, {
      kind: 'mobile-money',
      mode_of_payment: 'MTN MoMo',
      amount: 25000,
      reference: 'MOMO-884422',
    });
    await cashierOutbox.replay();

    const settleCall = sent.find((entry) => entry.url.includes('make_invoice'));
    expect(settleCall).toBeTruthy();
    expect(settleCall!.body.payments).toEqual([
      expect.objectContaining({
        mode_of_payment: 'MTN MoMo',
        amount: 25000,
        reference_no: 'MOMO-884422',
      }),
    ]);
  });

  it('caches waiter read endpoints in runtime-caching.ts while staying strictly same-origin GET', async () => {
    const caching = await import('../src/pwa/runtime-caching');
    const origin = 'https://pos.example.test';
    for (const path of [
      '/api/method/ury.ury_pos.api.getBranch',
      '/api/method/ury.ury_pos.api.getTable?room=Main+Hall',
      '/api/method/ury.ury_pos.api.getTablesForBranch?branch=Kampala',
      '/api/method/ury.ury_pos.api.getRoom',
      '/api/method/ury.ury_pos.api.getBranchRoom?branch=Kampala',
      '/api/method/ury.ury_pos.api.get_captain_context',
    ]) {
      expect(caching.requestMatchesRuntimeCache(`${origin}${path}`, 'GET', origin), path).toBe(true);
      expect(caching.requestMatchesRuntimeCache(`${origin}${path}`, 'POST', origin), path).toBe(false);
    }
  });

  it('syncs offline outbox snapshots across tabs via BroadcastChannel and optional LAN peer relay', async () => {
    const peerSyncMod = await import('../src/offline/peer-sync');
    expect(peerSyncMod.createOfflinePeerSync).toBeTypeOf('function');

    const channels = new Map<string, Set<(event: MessageEvent) => void>>();
    class MockBroadcastChannel {
      name: string;
      onmessage: ((event: MessageEvent) => void) | null = null;
      private listeners = new Set<(event: MessageEvent) => void>();
      constructor(name: string) {
        this.name = name;
        if (!channels.has(name)) channels.set(name, new Set());
        channels.get(name)!.add((ev) => {
          this.onmessage?.(ev);
          this.listeners.forEach((l) => l(ev));
        });
      }
      addEventListener(_type: string, cb: (event: MessageEvent) => void) {
        this.listeners.add(cb);
      }
      removeEventListener(_type: string, cb: (event: MessageEvent) => void) {
        this.listeners.delete(cb);
      }
      postMessage(data: unknown) {
        for (const handler of channels.get(this.name) || []) {
          handler({ data } as MessageEvent);
        }
      }
      close() {}
    }
    vi.stubGlobal('BroadcastChannel', MockBroadcastChannel);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, peers: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const received: unknown[] = [];
    const peerA = peerSyncMod.createOfflinePeerSync({
      branch: 'Kampala',
      posProfile: 'Main Till',
      relayUrl: 'http://192.168.1.50:8080/relay',
      onPeerUpdate: (msg: unknown) => received.push(msg),
    });
    const peerB = peerSyncMod.createOfflinePeerSync({
      branch: 'Kampala',
      posProfile: 'Main Till',
      onPeerUpdate: (msg: unknown) => received.push(msg),
    });

    await peerA.broadcast({
      type: 'outbox_snapshot',
      sender: 'waiter-1',
      pendingTables: [{ table: 'T-05', clientOrderId: 'abc12345', waiter: 'waiter-1', itemCount: 2, updatedAt: '2026-11-24T12:00:00Z' }],
    });

    expect(received.length).toBeGreaterThanOrEqual(1);
    expect(fetchMock).toHaveBeenCalledWith(
      'http://192.168.1.50:8080/relay',
      expect.objectContaining({ method: 'POST' }),
    );
    peerA.close();
    peerB.close();
  });
});

describe('POS-PR-6: G22 & G25 ClosingPaymentTable currency-wise foreign cash count', () => {
  it('renders native foreign currency input, expected amount, and base equivalent for FX tender rows', async () => {
    const { default: ClosingPaymentTable } = await import('../src/components/ClosingPaymentTable');
    const onChange = vi.fn();
    const onForeignChange = vi.fn();

    render(
      <ClosingPaymentTable
        currency="UGX"
        touchedModes={new Set()}
        onChange={onChange}
        onForeignChange={onForeignChange}
        rows={[
          {
            mode_of_payment: 'Cash',
            opening_amount: 50000,
            expected_amount: 30000,
            closing_amount: 30000,
            difference: 0,
          },
          {
            mode_of_payment: 'USD Cash',
            opening_amount: 0,
            expected_amount: 370000,
            closing_amount: 370000,
            difference: 0,
            custom_tender_currency: 'USD',
            custom_expected_foreign_amount: 100,
            custom_closing_foreign_amount: 100,
            custom_foreign_difference: 0,
            effective_exchange_rate: 3700,
          } as any,
        ]}
      />,
    );

    const fxInput = screen.getByTestId('closing-fx-input-USD Cash') as HTMLInputElement;
    expect(fxInput).toBeInTheDocument();
    expect(screen.getByText(/USD/)).toBeInTheDocument();

    fireEvent.change(fxInput, { target: { value: '95' } });
    await waitFor(() => {
      expect(onForeignChange).toHaveBeenCalledWith('USD Cash', 95);
    });
  });
});

describe('POS-PR-6: 4-locale i18n coverage and zero vendor strings', () => {
  const REQUIRED_KEYS: Record<string, string> = {
    'payment.reference_label_default': '',
    'payment.reference_placeholder_default': '',
    'payment.reference_required': '{{mode}}',
    'payment.reference_invalid': '{{mode}}',
    'payment.reference_date': '',
    'payment.reference_date_required': '{{mode}}',
    'payment.fx_rate_badge': '{{currency}},{{home}},{{rate}}',
    'payment.fx_equivalent': '{{amount}}',
    'payment.fx_stale_rate': '{{currency}}',
    'payment.fx_override_rate': '',
    'payment.fx_change_local': '{{amount}},{{currency}}',
    'pos_closing.fx_native_count': '{{currency}}',
    'pos_closing.fx_base_equivalent': '{{amount}}',
    'offline.waiter_offline_pill': '',
    'offline.waiter_queued_badge': '{{count}}',
    'offline.waiter_settle_blocked': '',
  };

  describe.each([
    ['en', en],
    ['fr', fr],
    ['es', es],
    ['ar', ar],
  ] as const)('%s locale', (_lang, locale) => {
    it.each(Object.entries(REQUIRED_KEYS))('has %s with matching placeholders and no vendor names', (key, expectedPlaceholders) => {
      const value = lookup(locale as Tree, key);
      expect(value?.trim(), key).toBeTruthy();
      expect(placeholders(value!)).toBe(expectedPlaceholders);
      expect(value!).not.toMatch(VENDOR_RE);
    });
  });
});
