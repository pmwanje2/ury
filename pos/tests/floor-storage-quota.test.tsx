import { BroadcastChannel as NodeChannel } from 'node:worker_threads';
import { IDBFactory } from 'fake-indexeddb';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as core from '@ury/core';
import AuthGuard from '../src/components/AuthGuard';
import OutboxPanel from '../src/components/OutboxPanel';
import { cashierOutbox, clearCashierStorage } from '../src/offline/cashier';
import { onOutboxMembershipChange } from '../src/offline/outbox';
import { useOutbox } from '../src/offline/use-outbox';
import { setFloorSessionMarker } from '../src/lib/floor-session';
import { testLocks } from './offline-locks';

const state = vi.hoisted(() => ({
  user: { name: 'owner@example.test', roles: ['Cashier'] },
  posProfile: { name: 'Till', custom_floor_stay_signed_in_roles: [{ role: 'Cashier' }], ury_offline_capture: 1 },
  checkAuth: vi.fn(), fetchPosProfile: vi.fn(), hasAccess: true,
}));
vi.mock('../src/store/root-store', () => ({ useRootStore: () => state }));

const capture = () => cashierOutbox(state.posProfile, state.user).capture({
  pos_profile: 'Till', no_of_pax: 1, order_type: 'Take Away', invoice: null, last_invoice: null,
  customer: 'Private order', items: [{ item: 'Tea', item_name: 'Tea', qty: 1, rate: 10 }],
}, 10);

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('navigator', { onLine: true, locks: testLocks() });
  vi.stubGlobal('BroadcastChannel', undefined);
  const original = Storage.prototype.setItem;
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (this === localStorage) throw new DOMException('Full', 'QuotaExceededError');
    original.call(this, key, value);
  });
  vi.spyOn(window, 'open').mockReturnValue(null);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('locks an admitted tab on its next poll without exposing or replaying the newly committed row', async () => {
  const queue = cashierOutbox(state.posProfile, state.user);
  const replay = vi.spyOn(queue, 'replay').mockResolvedValue();
  const timer = vi.spyOn(window, 'setInterval');
  const observed: string[] = [];
  function Workspace() {
    const { rows } = useOutbox(state.posProfile, state.user, true);
    observed.push(...rows.map(row => row.client_order_id));
    return <main>Workspace<OutboxPanel posProfile={state.posProfile} user={state.user} /></main>;
  }
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByText('Workspace');
  await act(async () => { await queue.list(); });
  // Commit as another window: its DOM events are not delivered to our window.
  const dispatch = vi.spyOn(window, 'dispatchEvent').mockReturnValue(true);
  try { await capture(); } finally { dispatch.mockRestore(); }
  replay.mockClear();
  const polls = timer.mock.calls.filter(([, delay]) => delay === 5000);
  expect(polls.length).toBeGreaterThan(0);
  await act(async () => { for (const [wake] of polls) await (wake as () => Promise<void>)(); });
  await screen.findByText(/account that owns the queued orders/);
  expect(observed).toEqual([]);
  expect(replay).not.toHaveBeenCalled();
  expect(document.querySelector('[data-client-order]')).toBeNull();
});

it('sends and receives cross-tab membership notifications despite localStorage quota', async () => {
  vi.stubGlobal('BroadcastChannel', NodeChannel);
  const remote = new NodeChannel('ury-outbox-membership');
  const received = vi.fn();
  remote.onmessage = received;
  const recheck = vi.fn();
  const unsubscribe = onOutboxMembershipChange(recheck);
  try {
    await capture();
    await waitFor(() => expect(received).toHaveBeenCalled());
    recheck.mockClear();
    remote.postMessage(null);
    await waitFor(() => expect(recheck).toHaveBeenCalled());
  } finally { unsubscribe(); remote.close(); }
});

it.each(['server logout', 'owner login'])('keeps a failed till logout across tab closure until successful %s', async (resolution) => {
  const logout = vi.spyOn(core, 'logout').mockResolvedValue(false);
  await expect(clearCashierStorage(true)).rejects.toThrow();
  expect(localStorage.getItem('ury_floor_logout_failed')).toBeNull();
  sessionStorage.clear();
  window.name = '';
  const Workspace = vi.fn(() => <main>Workspace</main>);
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(Workspace).not.toHaveBeenCalled();
  expect(logout).toHaveBeenCalledTimes(2);
  if (resolution === 'server logout') {
    logout.mockResolvedValue(true);
    await clearCashierStorage(true);
  } else {
    vi.spyOn(core.auth, 'loginWithUsernamePassword').mockResolvedValue({ message: 'Logged In' } as never);
    vi.spyOn(core.auth, 'getLoggedInUser').mockResolvedValue(state.user.name);
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: state.user.name } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'test password' } });
    fireEvent.submit(screen.getByLabelText('Password').closest('form')!);
    await waitFor(() => expect(core.auth.getLoggedInUser).toHaveBeenCalled());
    // A second transaction under the shared login lock waits for sign-in cleanup to commit.
    const { withEmptyOutbox } = await import('../src/offline/outbox');
    await withEmptyOutbox(async () => {});
  }
  cleanup();
  sessionStorage.clear();
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByText('Workspace');
});

it('fails closed on a durable-store read error even with a confirmed tab', async () => {
  setFloorSessionMarker(state.user.name, false);
  vi.spyOn(indexedDB, 'open').mockImplementation(() => { throw new Error('Storage unavailable'); });
  const Workspace = vi.fn(() => <main>Workspace</main>);
  render(<AuthGuard><Workspace /></AuthGuard>);
  await screen.findByLabelText('Password');
  expect(Workspace).not.toHaveBeenCalled();
});
