import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { initI18n } from '../src/i18n';
import { usePOSStore } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';

let root: Root; let container: HTMLDivElement;
const profile = { name: 'Till A', ury_offline_capture: 1, role_allowed_for_billing: [{ role: 'Cashier' }] };
const user = { name: 'cashier@example.com', roles: ['Cashier'] };

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('navigator', { onLine: false, storage: { persist: vi.fn(async () => true) } });
  usePOSStore.setState({ posProfile: profile as any }); useRootStore.setState({ user: user as any });
  await initI18n('en'); container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

test('the cashier banner exposes pending count and a Needs review reason without hiding the queue', async () => {
  const { OfflineOutbox } = await import('../src/offline/outbox');
  const queue = new OfflineOutbox({ profile, user, print: async () => true });
  await queue.capture({ pos_profile: 'Till A', customer: 'Walk In', items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 100 }], no_of_pax: 1, invoice: null, last_invoice: null, order_type: 'Take Away' }, 100);
  const present = existsSync(resolve('pos/src/components/OfflineBanner.tsx'));
  expect(present, 'the offline banner is missing').toBe(true);
  if (!present) throw new Error('expected RED failure above');
  const path = '../src/components/OfflineBanner.tsx';
  const { default: OfflineBanner } = await import(/* @vite-ignore */ path);
  await act(async () => root.render(<OfflineBanner />));
  await vi.waitFor(() => expect(container.textContent).toContain('1 pending'));
  expect(container.textContent).toContain('Offline capture');
  expect(container.querySelector('[data-testid="offline-banner"]')).not.toBeNull();
});

test('a parked server refusal is shown as Needs review with its reason', async () => {
  const { OfflineOutbox } = await import('../src/offline/outbox');
  const q = new OfflineOutbox({ profile, user, print: () => true, request: async (url) => new Response(JSON.stringify(
    String(url).includes('get_logged_user') ? { message: user.name } : { exc_type: 'ValidationError', exception: 'URY_OFFLINE_OUTSIDE_SHIFT' }),
    { status: String(url).includes('get_logged_user') ? 200 : 417, headers: { 'Content-Type': 'application/json' } }) });
  await q.capture({ pos_profile: profile.name, items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 100 }], no_of_pax: 1, invoice: null, last_invoice: null, order_type: 'Take Away' }, 100);
  await q.replay();
  const path = '../src/components/OfflineBanner.tsx';
  expect(existsSync(resolve('pos/src/components/OfflineBanner.tsx'))).toBe(true);
  const { default: Banner } = await import(/* @vite-ignore */ path);
  await act(async () => root.render(<Banner />));
  await vi.waitFor(() => expect(container.textContent).toContain('1 Needs review'));
  expect(container.textContent).toContain('URY_OFFLINE_OUTSIDE_SHIFT');
});
